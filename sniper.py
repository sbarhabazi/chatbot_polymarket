import asyncio

from web3 import Web3

from config import BotConfig
from contracts.abis import CHAINLINK_AGGREGATOR_ABI
from contracts.addresses import CHAINLINK_BTC_USD
from logger import log
from market_manager import MarketManager, compute_window_timestamp
from models import MarketInfo
from price_analyzer import PriceAnalyzer
from wallet_manager import WalletManager, calculate_net_yield


class Sniper:
    """Process A: Distance-based Safe Scalper."""

    def __init__(
        self,
        config: BotConfig,
        market_manager: MarketManager,
        price_analyzer: PriceAnalyzer,
        wallet_manager: WalletManager,
        clob_client=None,
        w3: Web3 | None = None,
    ):
        self._config = config
        self._mm = market_manager
        self._pa = price_analyzer
        self._wm = wallet_manager
        self._client = clob_client
        self._last_traded_window: int | None = None
        self._last_observed_window: int | None = None
        self._paper_trades: list[dict] = []
        self._chainlink_open_price: float | None = None

        # Chainlink oracle (same source Polymarket uses for resolution)
        self._chainlink = None
        if w3:
            self._chainlink = w3.eth.contract(
                address=Web3.to_checksum_address(CHAINLINK_BTC_USD),
                abi=CHAINLINK_AGGREGATOR_ABI,
            )

    async def run(self) -> None:
        mode = "PAPER" if self._config.paper_trading else "LIVE"
        log.info("Sniper (Process A) started [%s] — Safe Scalper v2", mode)
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

        # Capture Chainlink BTC price on first observation of new window
        if self._last_observed_window != current_window:
            self._chainlink_open_price = self._get_chainlink_price()
            self._last_observed_window = current_window
            if self._chainlink_open_price:
                log.info("Window %d Chainlink BTC: $%.2f (%.0fs left)",
                         current_window, self._chainlink_open_price, remaining)

        # Observation phase
        if not self._mm.is_in_entry_window(market):
            btc = self._pa.get_current_price()
            btc_str = f"${btc:,.2f}" if btc else "N/A"

            # Try to get price_to_beat (check API every ~60s, not every 5s)
            ptb = market.price_to_beat
            if ptb is None and int(remaining) % 60 < 6:
                refreshed = await self._mm.get_market_by_window_ts(market.window_ts)
                if refreshed and refreshed.price_to_beat:
                    ptb = refreshed.price_to_beat

            ptb_str = f"beat=${ptb:,.2f}" if ptb else "beat=N/A"
            dist = ""
            if btc and ptb:
                d = btc - float(ptb)
                dist = f"dist=${d:+.1f}"

            log.info(
                "Observing %d | %.0fs left | BTC: %s | %s %s | Up=$%.3f Down=$%.3f",
                current_window, remaining, btc_str, ptb_str, dist,
                market.up_price, market.down_price,
            )
            await asyncio.sleep(5)
            return

        # Entry window
        await self._attempt_entry(market)
        self._last_traded_window = current_window
        self._window_open_price = None  # Reset for next window

        remaining = max(0, self._mm.seconds_remaining(market))
        log.info("Sleeping %.0fs until market close", remaining)
        await asyncio.sleep(remaining + 2)

    async def _attempt_entry(self, market: MarketInfo) -> None:
        remaining = self._mm.seconds_remaining(market)
        log.info(
            "=== ENTRY WINDOW [%s] === %.0fs remaining | window %d",
            "EARLY" if remaining > 60 else "LATE",
            remaining, market.window_ts,
        )

        # Check 1: price_to_beat available?
        # Priority: Polymarket API > Chainlink oracle
        ptb = market.price_to_beat
        if ptb is None:
            refreshed = await self._mm.get_market_by_window_ts(market.window_ts)
            if refreshed and refreshed.price_to_beat:
                ptb = refreshed.price_to_beat

        if ptb is None and self._chainlink_open_price:
            ptb = self._chainlink_open_price
            log.info("Using Chainlink oracle price: $%.2f", ptb)

        if ptb is None:
            log.info("SKIP: no priceToBeat (API or Chainlink)")
            return

        price_to_beat = float(ptb)

        # Check 2: PriceAnalyzer connected?
        if not self._pa.is_connected:
            log.warning("SKIP: PriceAnalyzer not connected")
            return

        # Check 3: Compute distance signal
        signal = self._pa.compute_distance_signal(
            price_to_beat=price_to_beat,
            seconds_remaining=remaining,
        )
        if signal is None:
            log.warning("SKIP: Insufficient price data")
            return

        log.info(
            "Signal: %s | BTC=$%.2f | beat=$%.2f | dist=$%.1f | "
            "conf=%.2f | mom=[%.1f,%.1f,%.1f] agree=%d/3 | vol=%.5f | bvr=%.2f",
            signal.recommended_side, signal.current_price, signal.price_to_beat,
            signal.btc_distance, signal.confidence,
            signal.momentum_10s, signal.momentum_30s, signal.momentum_60s,
            signal.momentum_agreement,
            signal.volatility_30s, signal.buy_volume_ratio,
        )

        # Check 4: Signal rejected?
        if signal.skip_reason:
            log.info("SKIP: %s", signal.skip_reason)
            return

        # Check 5: Confidence threshold
        if signal.confidence < self._config.medium_confidence_threshold:
            log.info(
                "SKIP: Confidence %.2f < minimum %.2f",
                signal.confidence, self._config.medium_confidence_threshold,
            )
            return

        # Determine token and price
        if signal.recommended_side == "Up":
            token_id = market.up_token_id
            share_price = market.up_price
        else:
            token_id = market.down_token_id
            share_price = market.down_price

        # Refresh prices
        refreshed = await self._mm.get_market_by_window_ts(market.window_ts)
        if refreshed and refreshed.accepting_orders:
            if signal.recommended_side == "Up":
                share_price = refreshed.up_price
            else:
                share_price = refreshed.down_price

        net_yield = calculate_net_yield(share_price)

        # Check 6: Price acceptable?
        if not WalletManager.yield_acceptable(share_price, self._config.max_buy_price):
            log.info("SKIP: Price $%.3f > max $%.2f", share_price, self._config.max_buy_price)
            return

        if WalletManager.yield_too_low(share_price, self._config.abort_price):
            log.warning("ABORT: Price $%.3f >= abort $%.2f", share_price, self._config.abort_price)
            return

        # Check 7: Liquidity
        if market.liquidity < self._config.min_market_liquidity:
            log.warning("SKIP: Liquidity $%.0f < min", market.liquidity)
            return

        # Check 8: Confidence-based position sizing
        balance = await self._wm.get_usdc_balance(force_refresh=True)

        if signal.confidence >= self._config.high_confidence_threshold:
            size_fraction = self._config.high_confidence_size
            tier = "HIGH"
        else:
            size_fraction = self._config.medium_confidence_size
            tier = "MEDIUM"

        bet_amount = round(balance * size_fraction, 2)
        if bet_amount < 5.0:
            log.warning("SKIP: Bet $%.2f < $5 min (balance=$%.2f)", bet_amount, balance)
            return

        log.info(
            "BUY [%s]: %s | $%.2f (%.0f%% of $%.2f) @ $%.3f | yield=%.1f%% | dist=$%.1f",
            tier, signal.recommended_side, bet_amount, size_fraction * 100,
            balance, share_price, net_yield * 100, signal.btc_distance,
        )

        # Execute
        if self._config.paper_trading:
            shares = bet_amount / share_price
            trade = {
                "window": market.window_ts,
                "side": signal.recommended_side,
                "price": share_price,
                "amount": bet_amount,
                "shares": shares,
                "potential_payout": shares * 1.0,
                "btc_price": signal.current_price,
                "btc_distance": signal.btc_distance,
                "confidence": signal.confidence,
                "tier": tier,
            }
            self._paper_trades.append(trade)
            self._wm.paper_deduct(bet_amount)
            log.info(
                "PAPER BUY OK: %.1f shares @ $%.3f | payout=$%.2f | trades=%d | bal=$%.2f",
                shares, share_price, shares, len(self._paper_trades),
                await self._wm.get_usdc_balance(),
            )
        else:
            success = await self._place_limit_buy(token_id, bet_amount, share_price)
            if success:
                self._wm.invalidate_cache()
                log.info("BUY FILLED at $%.2f (real yield: %.1f%%)", share_price, net_yield * 100)
            else:
                log.warning("BUY NOT FILLED (no sellers at $%.2f)", share_price)

    async def _place_limit_buy(self, token_id: str, amount_usdc: float, price: float) -> bool:
        """Place a LIMIT buy order at the specified price.

        Unlike market orders that eat the entire order book (causing massive slippage),
        limit orders only fill at the specified price or better.

        Args:
            token_id: The Polymarket token ID
            amount_usdc: USDC to spend
            price: Max price per share (e.g. 0.55 = $0.55/share)
        """
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            # Calculate number of shares: amount_usdc / price
            # Polymarket requires: price = 2 decimals, size = whole number
            price = round(price, 2)
            size = int(amount_usdc / price)  # Floor to whole shares

            if size < 1:
                log.warning("Order size too small: %d shares", size)
                return False

            log.info(
                "LIMIT ORDER: %d shares @ $%.2f = $%.2f",
                size, price, size * price,
            )

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            )
            signed_order = self._client.create_order(order_args)
            # GTC: order stays in book, partial fills OK. Market close auto-cancels unfilled.
            response = self._client.post_order(signed_order, OrderType.GTC)

            if isinstance(response, dict):
                status = response.get("status", "")
                if status == "matched":
                    taking = response.get("takingAmount", "?")
                    making = response.get("makingAmount", "?")
                    log.info("LIMIT FILLED: paid $%s for %s shares", making, taking)
                    return True
                else:
                    log.warning("Limit order not filled: %s (no sellers at $%.2f)", status, price)
                    return False

            log.info("Order response: %s", response)
            return True
        except Exception as e:
            log.error("Order execution error: %s", e, exc_info=True)
            return False

    def _get_chainlink_price(self) -> float | None:
        """Get BTC/USD price from Chainlink oracle (same source as Polymarket resolution)."""
        if not self._chainlink:
            return None
        try:
            data = self._chainlink.functions.latestRoundData().call()
            price = data[1] / 1e8  # Chainlink uses 8 decimals
            return price
        except Exception as e:
            log.warning("Chainlink oracle error: %s", e)
            return None
