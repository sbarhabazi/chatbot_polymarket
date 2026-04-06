import asyncio

from config import BotConfig
from logger import log
from market_manager import MarketManager, compute_window_timestamp
from models import MarketInfo
from price_analyzer import PriceAnalyzer
from wallet_manager import WalletManager, calculate_net_yield


class Sniper:
    """Process A: Observe price action, apply entry rules, execute buy."""

    def __init__(
        self,
        config: BotConfig,
        market_manager: MarketManager,
        price_analyzer: PriceAnalyzer,
        wallet_manager: WalletManager,
        clob_client=None,
    ):
        self._config = config
        self._mm = market_manager
        self._pa = price_analyzer
        self._wm = wallet_manager
        self._client = clob_client
        self._last_traded_window: int | None = None
        # Paper trading stats
        self._paper_trades: list[dict] = []

    async def run(self) -> None:
        mode = "PAPER" if self._config.paper_trading else "LIVE"
        log.info("Sniper (Process A) started [%s]", mode)
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
            log.info("No active market found, retrying in 10s")
            await asyncio.sleep(10)
            return

        remaining = self._mm.seconds_remaining(market)

        if remaining <= 0:
            await asyncio.sleep(1)
            return

        if not self._mm.is_in_entry_window(market):
            btc = self._pa.get_current_price()
            btc_str = f"${btc:,.2f}" if btc else "N/A"
            log.info(
                "Observing %d | %.0fs left | BTC: %s | Up=$%.3f Down=$%.3f",
                current_window, remaining, btc_str,
                market.up_price, market.down_price,
            )
            await asyncio.sleep(5)
            return

        await self._attempt_entry(market)
        self._last_traded_window = current_window

        remaining = max(0, self._mm.seconds_remaining(market))
        log.info("Sleeping %.0fs until market close", remaining)
        await asyncio.sleep(remaining + 2)

    async def _attempt_entry(self, market: MarketInfo) -> None:
        remaining = self._mm.seconds_remaining(market)
        log.info(
            "=== ENTRY WINDOW === %.0fs remaining | window %d",
            remaining, market.window_ts,
        )

        # Check 1: Price analyzer connected?
        if not self._pa.is_connected:
            log.warning("SKIP: PriceAnalyzer not connected")
            return

        # Check 2: Compute signal
        signal = self._pa.compute_signal()
        if signal is None:
            log.warning("SKIP: Insufficient price data for signal")
            return

        log.info(
            "Signal: side=%s momentum=%.2f vol=%.5f confidence=%.2f dir=%s",
            signal.recommended_side, signal.momentum,
            signal.volatility_30s, signal.confidence, signal.price_direction,
        )

        # Check 3: Extreme volatility?
        if signal.is_extreme_volatility:
            log.warning("CANCEL: Extreme volatility detected (%.5f)", signal.volatility_30s)
            return

        # Check 4: Resistance wall?
        if signal.is_resistance_wall:
            log.warning("CANCEL: Resistance wall detected")
            return

        # Determine which token to buy
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
        log.info(
            "Target: %s | price=$%.3f | net yield=%.1f%%",
            signal.recommended_side, share_price, net_yield * 100,
        )

        # Check 5: Yield > 20%?
        if not WalletManager.yield_acceptable(share_price, self._config.max_buy_price):
            log.info(
                "SKIP: Price $%.3f > max $%.2f (yield too low)",
                share_price, self._config.max_buy_price,
            )
            return

        # Check 6: Yield not <= 1%?
        if WalletManager.yield_too_low(share_price, self._config.abort_price):
            log.warning("ABORT: Price $%.3f >= abort $%.2f", share_price, self._config.abort_price)
            return

        # Check 7: Liquidity
        if market.liquidity < self._config.min_market_liquidity:
            log.warning("SKIP: Liquidity $%.0f < min $%.0f", market.liquidity, self._config.min_market_liquidity)
            return

        # Check 8: Position sizing
        balance = await self._wm.get_usdc_balance(force_refresh=True)
        bet_amount = self._wm.calculate_bet_amount(balance)
        if bet_amount <= 0:
            log.warning("SKIP: Balance $%.2f too low for min bet ($5)", balance)
            return

        log.info(
            "EXECUTING BUY: %s | $%.2f (40%% of $%.2f) @ $%.3f",
            signal.recommended_side, bet_amount, balance, share_price,
        )

        # --- PAPER vs LIVE ---
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
            }
            self._paper_trades.append(trade)
            self._wm.paper_deduct(bet_amount)
            log.info(
                "PAPER BUY FILLED: %s %.1f shares @ $%.3f | potential payout: $%.2f",
                signal.recommended_side, shares, share_price, shares,
            )
            log.info(
                "Paper stats: %d trades | balance: $%.2f",
                len(self._paper_trades), await self._wm.get_usdc_balance(),
            )
        else:
            success = await self._place_market_buy(token_id, bet_amount)
            if success:
                self._wm.invalidate_cache()
                log.info("BUY ORDER FILLED")
            else:
                log.error("BUY ORDER FAILED")

    async def _place_market_buy(self, token_id: str, amount: float) -> bool:
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
            )
            signed_order = self._client.create_market_order(order_args)
            response = self._client.post_order(signed_order, OrderType.FOK)

            if isinstance(response, dict):
                status = response.get("status", "")
                if status == "matched":
                    log.info("Order matched: %s", response)
                    return True
                else:
                    log.warning("Order not matched: %s | %s", status, response)
                    return False

            log.info("Order response: %s", response)
            return True

        except Exception as e:
            log.error("Order execution error: %s", e, exc_info=True)
            return False
