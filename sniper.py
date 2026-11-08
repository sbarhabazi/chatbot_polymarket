from __future__ import annotations

import asyncio
import json
import time

from web3 import Web3

from balance_guard import BalanceGuard
from config import BotConfig
from learning_engine import LearningEngine, TradeRecord
from logger import log
from market_manager import MarketManager, compute_window_timestamp
from models import MarketInfo
from paths import data_path
from price_analyzer import PriceAnalyzer
from probability import (
    cost_per_share,
    edge,
    final_distribution,
    kelly_fraction,
    prob_up,
    taker_fee,
)
from reference_feed import ReferenceFeed
from wallet_manager import (
    POLY_MIN_SHARES,
    WalletManager,
    compute_offer_price,
    compute_order_shares,
)

SHADOW_LOG_FILE = data_path("shadow_log.jsonl")
SHADOW_CHECKPOINTS = (120, 90, 60, 30, 15)  # secondes restantes


class Sniper:
    """Process A : achète un côté seulement si la probabilité du modèle dépasse
    le prix du carnet, frais compris, d'au moins MIN_EDGE."""

    def __init__(
        self,
        config: BotConfig,
        market_manager: MarketManager,
        price_analyzer: PriceAnalyzer,
        wallet_manager: WalletManager,
        clob_client=None,
        w3: Web3 | None = None,
        learning_engine: LearningEngine | None = None,
        reference_feed: ReferenceFeed | None = None,
    ):
        self._config = config
        self._mm = market_manager
        self._pa = price_analyzer
        self._wm = wallet_manager
        self._client = clob_client
        self._le = learning_engine or LearningEngine()
        self._ref = reference_feed
        self._last_traded_window: int | None = None
        self._last_observed_window: int | None = None
        self._paper_trades: list[dict] = []
        # (window_ts, priceToBeat estimé, source) de la fenêtre en cours
        self._window_open: tuple[int, float, str] | None = None
        self._order_attempted_this_window: bool = False  # 1 ordre max par fenêtre
        self._shadow_done: set[tuple[int, int]] = set()

        # Defensive: prevent any possibility of double-trade in the same window
        self._order_lock = asyncio.Lock()
        self._order_pending_window: int | None = None
        self._active_gtc_order_id: str | None = None  # Pour annulation si non rempli
        self._active_gtc_context: tuple[str, float] | None = None  # (côté, priceToBeat)

    @property
    def _mode(self) -> str:
        return self._config.reference_feed

    async def run(self) -> None:
        mode = "PAPER" if self._config.paper_trading else "LIVE"
        log.info("Sniper (Process A) started [%s] — modèle de probabilité, référence=%s, "
                 "edge min=%.2f", mode, self._mode, self._config.min_edge)
        while True:
            try:
                await self._process_one_window()
            except Exception as e:
                log.error("Sniper loop error: %s", e, exc_info=True)
                await asyncio.sleep(5)

    # ─── Prix de référence ────────────────────────────────────

    def _current_reference(self) -> float | None:
        """Prix actuel dans l'unité de la référence (Chainlink ou Binance)."""
        if self._mode == "binance":
            return self._pa.get_current_price()
        return self._ref.latest("spot") if self._ref else None

    def _reference_open(self, window_ts: int) -> tuple[float | None, str]:
        """priceToBeat estimé : valeur de la référence à l'ouverture de la fenêtre."""
        if self._mode == "binance":
            return self._pa.price_at(window_ts), self._pa.active_source
        if not self._ref:
            return None, "chainlink"
        kind = "twap" if self._mode == "chainlink_twap" else "spot"
        return self._ref.value_at(kind, window_ts), f"chainlink-{kind}"

    async def _capture_window_open(self, window_ts: int, remaining: float) -> None:
        """Mémorise le priceToBeat estimé de la fenêtre (une fois par fenêtre)."""
        price, source = self._reference_open(window_ts)
        origin = "historique"
        if price is None and self._mode == "binance" and source == "Binance":
            price = await self._pa.fetch_binance_price_at(window_ts)
            origin = "API Binance"
        if price is None:
            if self._last_observed_window != window_ts:
                log.warning("Window %d: pas de prix d'ouverture %s fiable — fenêtre sautée",
                            window_ts, source)
            self._last_observed_window = window_ts
            return
        self._window_open = (window_ts, price, source)
        self._last_observed_window = window_ts
        log.info("Window %d open=$%.2f (%s, %s, %.0fs left)",
                 window_ts, price, source, origin, remaining)
        self._shadow({
            "kind": "open", "window_ts": window_ts, "mode": self._mode,
            "twap": self._ref.value_at("twap", window_ts) if self._ref else None,
            "spot": self._ref.value_at("spot", window_ts) if self._ref else None,
            "binance": self._pa.price_at(window_ts),
        })

    def _model(self, window_ts: int, seconds_left: float, price_to_beat: float):
        """(q_up, mean, sd, current) ou None si les données manquent."""
        current = self._current_reference()
        if current is None:
            return None
        if self._mode == "binance":
            sigma = self._pa.volatility_per_sqrt_s(300.0)
            twap, observed, basis = False, None, self._config.basis_noise
        else:
            sigma = self._ref.volatility("spot", 300.0)
            twap = self._mode == "chainlink_twap"
            end = window_ts + 300
            observed = (self._ref.average("spot", end - 60, time.time())
                        if twap and seconds_left < 60 else None)
            basis = 0.0
        if sigma is None or sigma <= 0:
            return None
        mean, sd = final_distribution(current, seconds_left, sigma, twap=twap,
                                      observed_window_avg=observed, basis_noise=basis)
        return prob_up(mean, sd, price_to_beat), mean, sd, current

    def _shadow(self, record: dict) -> None:
        """Journal de calibration : une ligne JSON par observation."""
        record["logged_at"] = time.time()
        try:
            with open(SHADOW_LOG_FILE, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            log.debug("Shadow log write failed: %s", e)

    # ─── Boucle par fenêtre ───────────────────────────────────

    async def _process_one_window(self) -> None:
        current_window = compute_window_timestamp()

        if self._last_traded_window == current_window:
            await asyncio.sleep(1)
            return

        market = await self._mm.get_current_market()
        if market is None:
            log.info("No active market, retrying in 10s")
            await asyncio.sleep(10)
            return

        remaining = self._mm.seconds_remaining(market)
        if remaining <= 0:
            await asyncio.sleep(1)
            return

        if self._window_open is None or self._window_open[0] != current_window:
            await self._capture_window_open(current_window, remaining)

        # Observation phase
        if not self._mm.is_in_entry_window(market):
            ref = self._current_reference()
            beat = self._window_open[1] if self._window_open and \
                self._window_open[0] == current_window else None
            dist = f"dist=${ref - beat:+.1f}" if ref and beat else ""
            log.info("Observing %d | %.0fs left | ref(%s): %s | open=%s %s",
                     current_window, remaining, self._mode,
                     f"${ref:,.2f}" if ref else "N/A",
                     f"${beat:,.2f}" if beat else "N/A", dist)
            await asyncio.sleep(5)
            return

        self._order_attempted_this_window = False
        traded = await self._scan_entry_window(market)
        self._last_traded_window = current_window
        self._order_pending_window = None

        remaining = max(0, self._mm.seconds_remaining(market))
        if traded:
            log.info("Trade placed, waiting %.0fs for market close", remaining)
            await self._monitor_gtc_order(remaining)
        else:
            log.info("No trade this window, %.0fs until close", remaining)
        await asyncio.sleep(max(0, self._mm.seconds_remaining(market)) + 2)

    async def _scan_entry_window(self, market: MarketInfo) -> bool:
        """Scanne toutes les 5 s pendant la fenêtre d'entrée ; un seul ordre au plus."""
        scan_count = 0
        while True:
            remaining = self._mm.seconds_remaining(market)
            if remaining <= 2:
                break
            if self._window_open is None or self._window_open[0] != market.window_ts:
                # Démarrage en cours de fenêtre : réessayer de capter le prix d'ouverture
                await self._capture_window_open(market.window_ts, remaining)
            if await self._attempt_entry(market, scan_count):
                return True
            if self._order_attempted_this_window:
                log.info("Ordre tenté cette fenêtre — pas de nouvel essai (1 ordre max/fenêtre)")
                return False
            scan_count += 1
            await asyncio.sleep(min(5, max(1, remaining - 2)))
        return False

    async def _attempt_entry(self, market: MarketInfo, scan_count: int = 0) -> bool:
        """Évalue Up et Down au prix du carnet ; achète le meilleur si son
        espérance (frais compris) dépasse MIN_EDGE. True si un ordre est placé."""
        remaining = self._mm.seconds_remaining(market)
        verbose = (scan_count == 0) or (scan_count % 3 == 0)

        window_open = self._window_open
        if not window_open or window_open[0] != market.window_ts:
            if verbose:
                log.info("SKIP: no window open price")
            return False
        if self._mode == "binance" and window_open[2] != self._pa.active_source:
            if verbose:
                log.warning("SKIP: price source changed (%s → %s) — distance non comparable",
                            window_open[2], self._pa.active_source)
            return False
        price_to_beat = window_open[1]

        model = self._model(market.window_ts, remaining, price_to_beat)
        if model is None:
            if verbose:
                log.info("SKIP: no reference price or volatility (%s)", self._mode)
            return False
        q_up, mean, sd, current = model

        up_book = await self._mm.get_order_book(market.up_token_id)
        down_book = await self._mm.get_order_book(market.down_token_id)
        ask_up = up_book.best_ask if up_book else None
        ask_down = down_book.best_ask if down_book else None

        # Journal de calibration aux points fixes (120, 90, 60, 30, 15 s restantes) :
        # on note le point qui vient d'être franchi, une seule fois par fenêtre.
        crossed = [cp for cp in SHADOW_CHECKPOINTS if remaining <= cp]
        if crossed:
            cp = min(crossed)
            if (market.window_ts, cp) not in self._shadow_done:
                self._shadow_done.update((market.window_ts, c) for c in crossed)
                self._shadow({
                    "kind": "quote", "window_ts": market.window_ts, "checkpoint": cp,
                    "seconds_left": remaining, "mode": self._mode,
                    "price_to_beat": price_to_beat, "current": current,
                    "mean": mean, "sd": sd, "q_up": q_up,
                    "ask_up": ask_up, "ask_down": ask_down,
                })

        candidates = []
        for side, q, book in (("Up", q_up, up_book), ("Down", 1.0 - q_up, down_book)):
            if book is None or book.best_ask is None:
                continue
            offer = compute_offer_price(book.best_ask, book.best_ask,
                                        self._config.ask_buffer, self._config.max_buy_price)
            if book.best_ask > self._config.max_buy_price:
                continue
            # Décision au prix limite (pire exécution possible), pas au meilleur ask
            candidates.append((edge(q, offer), side, q, book, offer))

        if verbose:
            log.info(
                "Scan #%d %.0fs | ref=$%.2f open=$%.2f (d=%+.1f, sd=%.1f) | P(Up)=%.3f | "
                "ask Up=%s Down=%s",
                scan_count, remaining, current, price_to_beat, current - price_to_beat, sd,
                q_up, f"{ask_up:.2f}" if ask_up else "-", f"{ask_down:.2f}" if ask_down else "-",
            )
        if not candidates:
            if verbose:
                log.info("  → SKIP: no ask ≤ $%.2f", self._config.max_buy_price)
            return False
        best_edge, side, q, book, offer_price = max(candidates, key=lambda c: c[0])
        if best_edge < self._config.min_edge:
            if verbose:
                log.info("  → SKIP: edge %+.3f < %.3f (meilleur: %s)", best_edge,
                         self._config.min_edge, side)
            return False

        token_id = market.up_token_id if side == "Up" else market.down_token_id
        share_price = book.best_ask

        # Liquidity
        if market.liquidity < self._config.min_market_liquidity:
            log.warning("SKIP: Liquidity $%.0f < min", market.liquidity)
            return False

        # Taille : Kelly sur la probabilité du modèle, plafonnée par le mode
        balance = await self._wm.get_usdc_balance(force_refresh=True)
        if self._config.paper_trading:
            guard_state = BalanceGuard.params_for_mode(BalanceGuard.get_mode_for_balance(balance))
        else:
            guard_state = BalanceGuard.read_current_mode()
        mode_label = guard_state["mode"]
        kelly_cap = guard_state["kelly_cap"]
        kelly_frac = guard_state["kelly_fraction"]
        if kelly_cap <= 0 or kelly_frac <= 0:
            log.info("SKIP: mode %s — aucun nouvel ordre (solde=$%.2f)", mode_label, balance)
            return False

        size_fraction = min(kelly_fraction(q, offer_price) * kelly_frac, kelly_cap)
        sizing_balance = (min(balance, self._config.paper_balance)
                          if self._config.paper_trading else balance)
        bet_amount = round(sizing_balance * size_fraction, 2)
        if bet_amount < 0.50:
            log.info("SKIP: Bet $%.2f < $0.50 (kelly=%.1f%%)", bet_amount, size_fraction * 100)
            return False

        current_window = market.window_ts
        if self._order_pending_window == current_window:
            return False

        async with self._order_lock:
            if self._last_traded_window == compute_window_timestamp():
                return False
            if self._order_pending_window == current_window:
                return False
            self._order_pending_window = current_window
            self._order_attempted_this_window = True

            log.info(
                "BUY [%s]: %s | P=%.3f ask=$%.2f limit=$%.2f edge=%+.3f | $%.2f "
                "(kelly=%.1f%% of $%.2f, cap=%.0f%%)",
                mode_label, side, q, share_price, offer_price, best_edge, bet_amount,
                size_fraction * 100, sizing_balance, kelly_cap * 100,
            )

            if self._config.paper_trading:
                shares = compute_order_shares(bet_amount, cost_per_share(offer_price),
                                              sizing_balance, kelly_cap)
                if shares < POLY_MIN_SHARES:
                    self._order_pending_window = None
                    log.info("PAPER SKIP: 5 parts @ $%.2f dépassent le plafond du mode", offer_price)
                    return False
                fill = book.fill_cost(shares, offer_price)
                if fill is None:
                    self._order_pending_window = None
                    log.info("PAPER SKIP: carnet insuffisant pour %d parts ≤ $%.2f", shares, offer_price)
                    return False
                avg_price = fill / shares
                cost = round(fill + shares * taker_fee(avg_price), 2)
                self._paper_trades.append({
                    "window": market.window_ts, "side": side, "price": avg_price,
                    "amount": cost, "shares": shares, "potential_payout": float(shares),
                    "prob": q, "edge": best_edge,
                })
                self._wm.paper_deduct(cost)
                self._last_traded_window = compute_window_timestamp()
                self._record_trade(market, side, q, current - price_to_beat, remaining,
                                   avg_price, cost, shares)
                log.info(
                    "PAPER BUY OK: %s %d parts @ $%.3f + frais = $%.2f | trades=%d | bal=$%.2f",
                    side, shares, avg_price, cost, len(self._paper_trades),
                    await self._wm.get_usdc_balance(),
                )
                return True

            placed = await self._place_limit_buy(
                token_id, bet_amount, share_price, q,
                best_ask=share_price, balance=balance, max_fraction=kelly_cap,
            )
            if placed is None:
                self._order_pending_window = None
                log.warning("Order failed at scan #%d", scan_count)
                return False

            committed_usdc, placed_price = placed
            if self._active_gtc_order_id:
                self._active_gtc_context = (side, price_to_beat)
            self._last_traded_window = compute_window_timestamp()
            self._wm.invalidate_cache()
            shares = round(committed_usdc / placed_price) if placed_price else None
            self._record_trade(market, side, q, current - price_to_beat, remaining,
                               placed_price, committed_usdc + (shares or 0) * taker_fee(placed_price),
                               shares)
            log.info("ORDER ACCEPTED (scan #%d) | %s $%.2f @ $%.3f | P=%.3f edge=%+.3f",
                     scan_count, side, committed_usdc, placed_price, q, best_edge)
            return True

    def _record_trade(self, market: MarketInfo, side: str, prob: float, distance: float,
                      seconds_remaining: float, share_price: float, amount_usdc: float,
                      shares: float | None) -> None:
        self._le.record_trade(TradeRecord(
            window_ts=market.window_ts,
            side=side,
            btc_distance=distance,
            seconds_remaining=seconds_remaining,
            confidence=prob,
            share_price=share_price,
            amount_usdc=amount_usdc,
            shares=shares,
            source="paper" if self._config.paper_trading else "live",
        ))

    GTC_REVERSAL_CANCEL_USD = 30.0  # BTC de l'autre côté du priceToBeat de plus de 30 $
    GTC_CHECK_INTERVAL = 10.0
    GTC_FINAL_CANCEL_SECONDS = 15.0  # annuler un ordre non rempli à 15 s de la fin

    async def _monitor_gtc_order(self, remaining_seconds: float) -> None:
        """Surveille un ordre GTC encore dans le carnet jusqu'à la fin de la fenêtre.

        Toutes les 10 s, l'ordre est annulé :
        - si BTC est passé de l'autre côté du priceToBeat de plus de 30 $
          (le pari n'est plus soutenu par le signal) ;
        - s'il reste 15 s ou moins, pour récupérer les fonds d'un ordre non rempli.
        Un ordre annulé sans aucun remplissage est retiré de l'historique par le
        Harvester (aucun token détenu).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, remaining_seconds)

        if not self._active_gtc_order_id or not self._client:
            await asyncio.sleep(max(0.0, remaining_seconds))
            return

        order_id = self._active_gtc_order_id
        side, price_to_beat = self._active_gtc_context or (None, None)
        self._active_gtc_order_id = None
        self._active_gtc_context = None

        log.info("GTC monitor: suivre ordre %s (%.0fs restantes)", order_id[:16], remaining_seconds)

        while True:
            time_left = deadline - loop.time()
            if time_left <= self.GTC_FINAL_CANCEL_SECONDS:
                log.warning(
                    "GTC monitor: %.0fs restantes, annulation preventive ordre %s",
                    time_left, order_id[:16],
                )
                await self._cancel_order(order_id)
                break

            btc = self._current_reference()
            if btc is not None and side and price_to_beat:
                against = (price_to_beat - btc) if side == "Up" else (btc - price_to_beat)
                if against > self.GTC_REVERSAL_CANCEL_USD:
                    log.warning(
                        "GTC monitor: BTC $%.2f a franchi le priceToBeat $%.2f contre %s "
                        "(-$%.0f) — annulation ordre %s",
                        btc, price_to_beat, side, against, order_id[:16],
                    )
                    await self._cancel_order(order_id)
                    break

            await asyncio.sleep(min(self.GTC_CHECK_INTERVAL,
                                    max(0.01, time_left - self.GTC_FINAL_CANCEL_SECONDS)))

        remaining = deadline - loop.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _cancel_order(self, order_id: str) -> bool:
        """Annule un ordre GTC dans le carnet."""
        try:
            resp = await asyncio.to_thread(self._client.cancel, order_id)
            log.info("GTC annule: %s | reponse: %s", order_id[:16], resp)
            self._wm.invalidate_cache()
            return True
        except Exception as e:
            log.warning("GTC annulation echouee (%s): %s", order_id[:16], e)
            return False

    async def _place_limit_buy(
        self,
        token_id: str,
        amount_usdc: float,
        price: float,
        confidence: float = 0.65,
        best_ask: float = 0.0,
        balance: float = 0.0,
        max_fraction: float = 0.0,
    ) -> tuple[float, float] | None:
        """Place a buy order priced just above best_ask for reliable fills.

        Strategy: offer max(best_ask, price) + ASK_BUFFER (default $0.02) to get
        priority in the order book without overpaying.

        Args:
            token_id: The Polymarket token ID
            amount_usdc: USDC to spend (Kelly sizing)
            price: Market price per share (e.g. 0.50 = $0.50/share)
            confidence: Signal confidence (0-1)
            best_ask: Best ask of THIS token (used as base for offer)
            balance: Current USDC.e balance
            max_fraction: Risk cap of the current mode; total cost never exceeds
                balance * max_fraction, even to reach Polymarket's 5-share minimum

        Returns:
            (USDC committed, offer price) when the order was matched or rests in
            the book, None when nothing was placed.
        """
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            order_type_str = self._config.order_type

            # Map string to OrderType enum
            order_type_map = {
                "FOK": OrderType.FOK,
                "GTC": OrderType.GTC,
                "GTD": OrderType.GTD,
                "FAK": OrderType.FAK,
            }
            order_type = order_type_map.get(order_type_str, OrderType.FOK)

            # Prix juste au-dessus du meilleur vendeur (ASK_BUFFER) pour être rempli
            offer_price = compute_offer_price(
                price, best_ask, self._config.ask_buffer, self._config.max_buy_price,
            )

            # Taille au coût réel par part (prix + frais taker) : le total reste
            # sous le plafond du mode, frais compris.
            size = compute_order_shares(amount_usdc, cost_per_share(offer_price),
                                        balance, max_fraction)
            if size < POLY_MIN_SHARES:
                log.warning(
                    "SKIP: minimum %d parts @ $%.2f = $%.2f dépasse le plafond du mode "
                    "(%.0f%% de $%.2f = $%.2f)",
                    POLY_MIN_SHARES, offer_price, POLY_MIN_SHARES * offer_price,
                    max_fraction * 100, balance, balance * max_fraction,
                )
                return None
            if size * offer_price > amount_usdc + 0.01:
                log.info(
                    "Ajustement taille au minimum Polymarket: %d parts | $%.2f → $%.2f",
                    size, amount_usdc, size * offer_price,
                )

            committed = round(size * offer_price, 2)
            log.info(
                "%s ORDER: %d shares @ $%.2f (market=$%.2f, ask=$%.2f, +$%.2f buffer) = $%.2f",
                order_type_str, size, offer_price, price, best_ask,
                self._config.ask_buffer, committed,
            )

            order_args = OrderArgs(
                token_id=token_id,
                price=offer_price,
                size=size,
                side=BUY,
            )
            # Appels HTTP synchrones : exécutés hors de la boucle asyncio
            signed_order = await asyncio.to_thread(self._client.create_order, order_args)
            response = await asyncio.to_thread(self._client.post_order, signed_order, order_type)

            if isinstance(response, dict):
                status = response.get("status", "")
                taking = response.get("takingAmount", "0")
                making = response.get("makingAmount", "0")
                order_id = response.get("orderID", response.get("id", "?"))

                log.info("Order response: status=%s taking=%s making=%s id=%s",
                         status, taking, making, order_id)

                if status == "matched":
                    log.info(
                        "%s FILLED: paid $%s for %s shares (offered $%.2f)",
                        order_type_str, making, taking, offer_price,
                    )
                    try:
                        paid = float(making) if making else committed
                    except (ValueError, TypeError):
                        paid = committed
                    return (paid if paid > 0 else committed), offer_price
                elif status == "live":
                    # GTC: order is in the book, waiting for sellers.
                    # Stocker l'order_id pour pouvoir l'annuler si non rempli
                    self._active_gtc_order_id = order_id
                    try:
                        filled = float(taking) if taking else 0
                    except (ValueError, TypeError):
                        filled = 0
                    if filled > 0:
                        log.info(
                            "%s PARTIAL FILL: %s shares filled, rest in book @ $%.2f | id=%s",
                            order_type_str, taking, offer_price, order_id,
                        )
                    else:
                        log.info(
                            "%s order PLACED in book @ $%.2f (waiting for sellers, may fill before close) | id=%s",
                            order_type_str, offer_price, order_id,
                        )
                    return committed, offer_price  # Stop scanning — order is live in the book
                else:
                    log.warning(
                        "%s order status=%s at $%.2f (full response: %s)",
                        order_type_str, status, offer_price, response,
                    )
                    return None

            # Réponse inattendue : on suppose l'ordre placé pour ne pas le doubler.
            # Si rien n'a été rempli, le Harvester retirera le trade de l'historique.
            log.warning("Order response (raw, unexpected format): %s", response)
            return committed, offer_price
        except Exception as e:
            err_msg = str(e)
            if "fully filled" in err_msg and "FOK" in err_msg:
                log.warning("FOK rejected (partial fill available, not enough for full order)")
            elif "not enough balance" in err_msg:
                log.warning("Insufficient balance for order (USDC locked in active orders)")
            else:
                log.error("Order execution error: %s", e, exc_info=True)
            return None
