from __future__ import annotations

import asyncio

from web3 import Web3

from balance_guard import BalanceGuard
from config import BotConfig
from contracts.abis import CHAINLINK_AGGREGATOR_ABI
from contracts.addresses import CHAINLINK_BTC_USD
from learning_engine import LearningEngine, TradeRecord
from logger import log
from market_manager import MarketManager, compute_window_timestamp
from models import MarketInfo
from price_analyzer import PriceAnalyzer
from wallet_manager import (
    POLY_MIN_SHARES,
    WalletManager,
    calculate_net_yield,
    compute_kelly_size,
    compute_offer_price,
    compute_order_shares,
)


class Sniper:
    """Process A: Composite Signal Scalper v5 (OBI + Kelly)."""

    def __init__(
        self,
        config: BotConfig,
        market_manager: MarketManager,
        price_analyzer: PriceAnalyzer,
        wallet_manager: WalletManager,
        clob_client=None,
        w3: Web3 | None = None,
        learning_engine: LearningEngine | None = None,
    ):
        self._config = config
        self._mm = market_manager
        self._pa = price_analyzer
        self._wm = wallet_manager
        self._client = clob_client
        self._le = learning_engine or LearningEngine()
        self._last_traded_window: int | None = None
        self._last_observed_window: int | None = None
        self._paper_trades: list[dict] = []
        self._chainlink_open_price: float | None = None
        self._price_source: str = "none"  # "api", "binance", "kraken", "chainlink"
        self._order_attempted_this_window: bool = False  # 1 ordre max par fenêtre

        # Defensive: prevent any possibility of double-trade in the same window
        self._order_lock = asyncio.Lock()
        self._order_pending_window: int | None = None
        self._active_gtc_order_id: str | None = None  # Pour annulation si non rempli
        self._active_gtc_context: tuple[str, float] | None = None  # (côté, priceToBeat)

        # Chainlink oracle (same source Polymarket uses for resolution)
        self._chainlink = None
        if w3:
            self._chainlink = w3.eth.contract(
                address=Web3.to_checksum_address(CHAINLINK_BTC_USD),
                abi=CHAINLINK_AGGREGATOR_ABI,
            )

    async def run(self) -> None:
        mode = "PAPER" if self._config.paper_trading else "LIVE"
        log.info("Sniper (Process A) started [%s] — Composite Signal v5", mode)
        log.info("LearningEngine: %s", self._le.summary())
        while True:
            try:
                await self._process_one_window()
            except Exception as e:
                log.error("Sniper loop error: %s", e, exc_info=True)
                await asyncio.sleep(5)

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

        # Capture reference BTC price on first observation of new window
        if self._last_observed_window != current_window:
            # Priority 1: Previous window's finalPrice (= current window's real priceToBeat)
            prev_final = await self._mm.get_previous_final_price(current_window)
            if prev_final:
                self._chainlink_open_price = prev_final
                self._price_source = "api"
                log.info("Window %d priceToBeat=$%.2f (VERIFIED from API, %.0fs left)",
                         current_window, prev_final, remaining)
            else:
                # Priority 2: Real-time price from active source (Binance/Kraken/Coinbase)
                live_price = self._pa.get_current_price()
                if live_price:
                    self._chainlink_open_price = live_price
                    self._price_source = self._pa._active_source.lower().split()[0]  # "binance", "kraken", "coinbase"
                    log.warning("Window %d %s BTC: $%.2f (APPROX — prev not resolved yet, %.0fs left)",
                                current_window, self._pa._active_source, live_price, remaining)
                else:
                    # Priority 3: Chainlink (least accurate)
                    self._chainlink_open_price = await self._get_chainlink_price()
                    self._price_source = "chainlink"
                    if self._chainlink_open_price:
                        log.warning("Window %d Chainlink: $%.2f (LEAST RELIABLE, %.0fs left)",
                                    current_window, self._chainlink_open_price, remaining)

            # Paper mode fallback
            if self._chainlink_open_price is None and self._config.paper_trading:
                self._chainlink_open_price = self._pa.get_current_price()
                self._price_source = "binance"

            self._last_observed_window = current_window

        # Observation phase
        if not self._mm.is_in_entry_window(market):
            btc = self._pa.get_current_price()
            btc_str = f"${btc:,.2f}" if btc else "N/A"

            # Try to upgrade priceToBeat from API or previous window (every ~30s)
            ptb = market.price_to_beat
            if ptb is None and int(remaining) % 30 < 6:
                refreshed = await self._mm.get_market_by_window_ts(market.window_ts)
                if refreshed and refreshed.price_to_beat:
                    ptb = refreshed.price_to_beat

            # Keep trying to upgrade to VERIFIED price if we only have an approximation
            if ptb is None and self._price_source != "api" and int(remaining) % 15 < 6:
                prev_final = await self._mm.get_previous_final_price(current_window)
                if prev_final:
                    old = self._chainlink_open_price
                    self._chainlink_open_price = prev_final
                    self._price_source = "api"
                    diff = prev_final - old if old else 0
                    log.info("UPGRADED priceToBeat=$%.2f (VERIFIED, was $%.2f, diff=$%.1f)",
                             prev_final, old or 0, diff)

            # Display beat from best available source
            beat = ptb or (self._chainlink_open_price if self._chainlink_open_price else None)
            ptb_str = f"beat=${beat:,.2f}" if beat else "beat=N/A"
            dist = ""
            if btc and beat:
                d = btc - float(beat)
                dist = f"dist=${d:+.1f}"

            log.info(
                "Observing %d | %.0fs left | BTC: %s | %s %s | Up=$%.3f Down=$%.3f",
                current_window, remaining, btc_str, ptb_str, dist,
                market.up_price, market.down_price,
            )
            await asyncio.sleep(5)
            return

        # Reset le flag d'ordre tenté pour cette fenêtre
        self._order_attempted_this_window = False

        # Entry window — scan continuously until trade or window closes
        traded = await self._scan_entry_window(market)
        # Mark window as traded (defensive: _attempt_entry already sets this on success,
        # but we also set it here to prevent re-entry even if no trade was placed)
        self._last_traded_window = current_window
        self._order_pending_window = None
        self._window_open_price = None  # Reset for next window

        remaining = max(0, self._mm.seconds_remaining(market))
        if traded:
            log.info("Trade placed, waiting %.0fs for market close", remaining)
            # Surveiller le GTC actif et l'annuler si le marché tourne contre nous
            await self._monitor_gtc_order(remaining)
        else:
            log.info("No trade this window, %.0fs until close", remaining)
        await asyncio.sleep(max(0, self._mm.seconds_remaining(market)) + 2)

    async def _scan_entry_window(self, market: MarketInfo) -> bool:
        """Continuously scan for entry opportunity during the entry window.

        Scans every 5s until a valid signal appears, then places ONE order
        and immediately returns — never multiple orders per window.
        """
        scan_count = 0
        order_attempted = False  # Garde-fou: un seul essai de placement par fenêtre

        while True:
            remaining = self._mm.seconds_remaining(market)
            if remaining <= 2:
                break

            result = await self._attempt_entry(market, scan_count)
            if result:
                return True  # Trade executed

            # Si le signal était assez fort pour tenter un ordre mais qu'il a échoué
            # (API error, rejet, etc.), on NE réessaie PAS dans cette fenêtre
            if hasattr(self, '_order_attempted_this_window') and self._order_attempted_this_window:
                log.info("Ordre tenté cette fenêtre — pas de nouvel essai (1 ordre max/fenêtre)")
                return False

            scan_count += 1
            # En attendant le prochain scan, vérifier si on vient d'entrer en fenêtre
            # → attendre 5s max (mais pas si la fenêtre se ferme)
            wait = min(5, max(1, remaining - 2))
            await asyncio.sleep(wait)

        return False

    async def _attempt_entry(self, market: MarketInfo, scan_count: int = 0) -> bool:
        """Attempt entry. Returns True if trade was placed, False otherwise."""
        remaining = self._mm.seconds_remaining(market)
        phase = "EARLY" if remaining > 60 else "LATE"

        if scan_count == 0:
            log.info(
                "=== ENTRY WINDOW [%s] === %.0fs remaining | window %d | SCANNING",
                phase, remaining, market.window_ts,
            )
        # Log every 3rd scan to avoid spam (or always for first scan)
        verbose = (scan_count == 0) or (scan_count % 3 == 0)

        # Check 1: price_to_beat available?
        # Priority: Polymarket API > Chainlink oracle
        ptb = market.price_to_beat
        if ptb is None:
            refreshed = await self._mm.get_market_by_window_ts(market.window_ts)
            if refreshed and refreshed.price_to_beat:
                ptb = refreshed.price_to_beat

        if ptb is None and self._chainlink_open_price:
            if self._price_source == "api":
                ptb = self._chainlink_open_price
                if verbose:
                    log.info("Using VERIFIED priceToBeat: $%.2f", ptb)
            else:
                # Try to upgrade to verified price first
                prev_final = await self._mm.get_previous_final_price(market.window_ts)
                if prev_final:
                    self._chainlink_open_price = prev_final
                    self._price_source = "api"
                    ptb = prev_final
                    log.info("UPGRADED to verified priceToBeat=$%.2f", ptb)
                else:
                    # Use approximate price if distance exceeds safety margin for the source.
                    # Binance/Kraken at window open ≈ real priceToBeat within $5-20
                    # Chainlink on-chain can lag by $50-80 vs Polymarket's resolution price
                    btc_now = self._pa.get_current_price()
                    if btc_now:
                        approx_dist = abs(btc_now - self._chainlink_open_price)
                        safety = 25 if self._price_source in ("binance", "kraken") else 80
                        if approx_dist > safety:
                            ptb = self._chainlink_open_price
                            if verbose:
                                log.info("Using APPROX %s=$%.2f (dist=$%.0f > $%d safety)",
                                         self._price_source, ptb, approx_dist, safety)
                        else:
                            if verbose:
                                log.info("WAIT: price not verified (%s), dist=$%.0f < $%d safety",
                                         self._price_source, approx_dist, safety)
                            return False
                    else:
                        return False

        # Paper trading: if still no priceToBeat, use current BTC price as approximation
        if ptb is None and self._config.paper_trading:
            btc_now = self._pa.get_current_price()
            if btc_now:
                ptb = btc_now
                self._chainlink_open_price = btc_now
                self._price_source = "approx"
                if verbose:
                    log.info("PAPER: using current BTC=$%.2f as priceToBeat (approx)", ptb)

        if ptb is None:
            if verbose:
                log.info("SKIP: no priceToBeat available")
            return False

        price_to_beat = float(ptb)

        # Check 2: PriceAnalyzer connected?
        if not self._pa.is_connected:
            if verbose:
                log.warning("SKIP: PriceAnalyzer not connected")
            return False

        # Check 3: Compute distance signal
        signal = self._pa.compute_distance_signal(
            price_to_beat=price_to_beat,
            seconds_remaining=remaining,
        )
        if signal is None:
            if verbose:
                log.warning("SKIP: Insufficient price data")
            return False

        if verbose:
            log.info(
                "Scan #%d [%s] %.0fs | %s dist=$%.1f | conf=%.2f (base=%.2f) | "
                "OBI=%+.2f | RSI=%.0f | signals=%d/%d",
                scan_count, phase, remaining,
                signal.recommended_side, signal.btc_distance,
                signal.confidence, signal.distance_confidence,
                signal.obi, signal.rsi,
                signal.signal_agreement, signal.signal_total,
            )

        # Check 4: Signal rejected?
        if signal.skip_reason:
            if verbose:
                log.info("  → SKIP: %s", signal.skip_reason)
            return False

        # Check 5: Confidence threshold
        if signal.confidence < self._config.medium_confidence_threshold:
            if verbose:
                log.info(
                    "  → conf %.2f < %.2f, waiting...",
                    signal.confidence, self._config.medium_confidence_threshold,
                )
            return False

        # Check 5b: Signal agreement — minimum 2/4 indicateurs concordants
        # Un signal 0/4 ou 1/4 est du bruit pur, pas une edge exploitable
        MIN_SIGNAL_AGREEMENT = 2
        if signal.signal_total > 0 and signal.signal_agreement < MIN_SIGNAL_AGREEMENT:
            if verbose:
                log.info(
                    "  → SKIP: signal_agreement %d/%d < %d minimum "
                    "(pas assez d'indicateurs concordants)",
                    signal.signal_agreement, signal.signal_total, MIN_SIGNAL_AGREEMENT,
                )
            return False

        # Determine token and price.
        # On joue uniquement le côté recommandé par le signal : si ce côté est trop
        # cher pour le capital, on saute la fenêtre (plus de bascule vers le côté opposé).
        side = signal.recommended_side
        token_id = market.up_token_id if side == "Up" else market.down_token_id
        share_price = market.up_price if side == "Up" else market.down_price

        # Refresh prices
        refreshed = await self._mm.get_market_by_window_ts(market.window_ts)
        if refreshed and refreshed.accepting_orders:
            share_price = refreshed.up_price if side == "Up" else refreshed.down_price

        net_yield = calculate_net_yield(share_price)

        # Check price acceptable?
        if not WalletManager.yield_acceptable(share_price, self._config.max_buy_price):
            log.info("SKIP: Price $%.3f > max $%.2f", share_price, self._config.max_buy_price)
            return False

        if WalletManager.yield_too_low(share_price, self._config.abort_price):
            log.warning("ABORT: Price $%.3f >= abort $%.2f", share_price, self._config.abort_price)
            return False

        # Check 7: Liquidity
        if market.liquidity < self._config.min_market_liquidity:
            log.warning("SKIP: Liquidity $%.0f < min", market.liquidity)
            return False

        # Check 8: Adaptive position sizing via BalanceGuard
        balance = await self._wm.get_usdc_balance(force_refresh=True)

        if self._config.paper_trading:
            # En papier, le BalanceGuard ne tourne pas : mode tiré du solde simulé
            # (un fichier d'état laissé par une session réelle ne compte pas).
            guard_state = BalanceGuard.params_for_mode(BalanceGuard.get_mode_for_balance(balance))
        else:
            # Lire le mode courant depuis le BalanceGuard (plafonds tirés du code)
            guard_state = BalanceGuard.read_current_mode()
        mode_label   = guard_state["mode"]
        kelly_cap    = guard_state["kelly_cap"]
        kelly_frac   = guard_state["kelly_fraction"]
        min_conf     = guard_state["min_confidence"]

        # Plancher absolu: $0.50
        abs_min_bet = 0.50

        if kelly_cap <= 0 or kelly_frac <= 0:
            log.info("SKIP: mode %s — aucun nouvel ordre (solde=$%.2f)", mode_label, balance)
            return False

        # Vérification de confiance minimale selon le mode
        if signal.confidence < min_conf:
            log.info(
                "SKIP: conf=%.2f < %.2f minimum (%s mode)",
                signal.confidence, min_conf, mode_label,
            )
            return False

        # Calcul Kelly avec paramètres du mode
        kelly_size = compute_kelly_size(
            confidence=signal.confidence,
            share_price=share_price,
            kelly_fraction=kelly_frac,
        )

        if kelly_size <= 0:
            log.info(
                "SKIP: EV négatif (conf=%.2f, price=$%.2f, mode=%s)",
                signal.confidence, share_price, mode_label,
            )
            return False

        size_fraction = min(kelly_size, kelly_cap)

        # Determine tier for logging
        if signal.confidence >= self._config.high_confidence_threshold:
            tier = "HIGH"
        elif signal.confidence >= self._config.medium_confidence_threshold:
            tier = "MEDIUM"
        else:
            tier = "LOW"

        bet_amount = round(balance * size_fraction, 2)

        # Plancher absolu: $0.50
        if bet_amount < abs_min_bet:
            log.warning(
                "SKIP: Bet $%.2f < $%.2f min absolu (balance=$%.2f, kelly=%.1f%%, mode=%s)",
                bet_amount, abs_min_bet, balance, kelly_size * 100, mode_label,
            )
            return False

        # Execute (guarded by lock to prevent double-trade)
        current_window = market.window_ts
        if self._order_pending_window == current_window:
            log.info("SKIP: order already pending for window %d", current_window)
            return False

        async with self._order_lock:
            # Re-check after acquiring lock
            if self._last_traded_window == compute_window_timestamp():
                log.info("SKIP: window already traded (caught by lock)")
                return False

            if self._order_pending_window == current_window:
                log.info("SKIP: order already pending for window %d", current_window)
                return False

            self._order_pending_window = current_window

            # Marquer que l'on tente un ordre dans cette fenêtre
            # (même si l'API rejette, on ne réessaiera pas cette fenêtre)
            self._order_attempted_this_window = True

            log.info(
                "BUY [%s/%s]: %s | $%.2f (kelly=%.1f%% of $%.2f, cap=%.0f%%) @ $%.3f | "
                "yield=%.1f%% | dist=$%.1f | signals=%d/%d",
                tier, mode_label, side, bet_amount, size_fraction * 100,
                balance, kelly_cap * 100, share_price, net_yield * 100, signal.btc_distance,
                signal.signal_agreement, signal.signal_total,
            )

            # Gamma `bestAsk` est celui du premier token (Up) : ne pas l'appliquer à Down.
            if side == "Up" and refreshed and refreshed.best_ask > 0:
                best_ask = refreshed.best_ask
            else:
                best_ask = share_price

            if self._config.paper_trading:
                # Simulation fidèle au réel : même prix offert, même nombre entier de
                # parts (minimum 5, plafond du mode). Hypothèse optimiste : l'ordre
                # est supposé rempli au prix offert.
                offer_price = compute_offer_price(
                    share_price, best_ask, self._config.ask_buffer, self._config.max_buy_price,
                )
                shares = compute_order_shares(bet_amount, offer_price, balance, kelly_cap)
                if shares < POLY_MIN_SHARES:
                    self._order_pending_window = None
                    log.info("PAPER SKIP: 5 parts @ $%.2f dépassent le plafond du mode", offer_price)
                    return False
                cost = round(shares * offer_price, 2)
                self._paper_trades.append({
                    "window": market.window_ts,
                    "side": side,
                    "price": offer_price,
                    "amount": cost,
                    "shares": shares,
                    "potential_payout": float(shares),
                    "btc_price": signal.current_price,
                    "btc_distance": signal.btc_distance,
                    "confidence": signal.confidence,
                    "tier": tier,
                })
                self._wm.paper_deduct(cost)
                self._last_traded_window = compute_window_timestamp()
                self._record_trade(market, side, signal, remaining, offer_price, cost)
                log.info(
                    "PAPER BUY OK: %s %d parts @ $%.2f = $%.2f | trades=%d | bal=$%.2f",
                    side, shares, offer_price, cost, len(self._paper_trades),
                    await self._wm.get_usdc_balance(),
                )
                return True

            placed = await self._place_limit_buy(
                token_id, bet_amount, share_price, signal.confidence,
                best_ask=best_ask, balance=balance, max_fraction=kelly_cap,
            )
            if placed is None:
                self._order_pending_window = None
                log.warning("Order failed at scan #%d", scan_count)
                return False

            committed_usdc, offer_price = placed
            if self._active_gtc_order_id:
                self._active_gtc_context = (side, price_to_beat)
            self._last_traded_window = compute_window_timestamp()
            self._wm.invalidate_cache()
            # Le trade n'entre dans l'historique d'apprentissage qu'une fois accepté
            # par le CLOB, avec le montant et le prix réellement engagés.
            self._record_trade(market, side, signal, remaining, offer_price, committed_usdc)
            log.info(
                "ORDER ACCEPTED (scan #%d) | %s $%.2f @ $%.3f | yield=%.1f%%",
                scan_count, side, committed_usdc, offer_price, net_yield * 100,
            )
            return True

    def _record_trade(self, market: MarketInfo, side: str, signal, seconds_remaining: float,
                      share_price: float, amount_usdc: float) -> None:
        self._le.record_trade(TradeRecord(
            window_ts=market.window_ts,
            side=side,
            btc_distance=signal.btc_distance,
            seconds_remaining=seconds_remaining,
            confidence=signal.confidence,
            share_price=share_price,
            amount_usdc=amount_usdc,
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

            btc = self._pa.get_current_price()
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

            size = compute_order_shares(amount_usdc, offer_price, balance, max_fraction)
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

    async def _get_chainlink_price(self) -> float | None:
        """Get BTC/USD price from Chainlink oracle (same source as Polymarket resolution)."""
        if not self._chainlink:
            return None
        try:
            data = await asyncio.to_thread(self._chainlink.functions.latestRoundData().call)
            price = data[1] / 1e8  # Chainlink uses 8 decimals
            return price
        except Exception as e:
            log.warning("Chainlink oracle error: %s", e)
            return None
