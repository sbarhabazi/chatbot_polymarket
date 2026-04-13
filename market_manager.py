from __future__ import annotations

import json
import ssl
import time
from datetime import datetime, timezone

import aiohttp
import certifi

from config import GAMMA_API_BASE, MARKET_INTERVAL_SECONDS, BotConfig
from logger import log
from models import MarketInfo


def compute_window_timestamp(unix_time: float | None = None) -> int:
    t = int(unix_time or time.time())
    return t - (t % MARKET_INTERVAL_SECONDS)


def build_slug(window_ts: int) -> str:
    return f"btc-updown-5m-{window_ts}"


class MarketManager:
    def __init__(self, config: BotConfig):
        self._config = config
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_current_market(self) -> MarketInfo | None:
        window_ts = compute_window_timestamp()
        return await self.get_market_by_window_ts(window_ts)

    async def get_previous_final_price(self, current_window_ts: int) -> float | None:
        """Get the finalPrice from the previous resolved window.

        In Polymarket 5-min BTC markets, priceToBeat of window N = finalPrice of window N-1.
        This is only available after the previous window resolves (usually within ~2 minutes).
        """
        prev_ts = current_window_ts - MARKET_INTERVAL_SECONDS
        market = await self.get_market_by_window_ts(prev_ts)
        if market is None:
            return None

        # Re-fetch to get the eventMetadata with finalPrice
        slug = build_slug(prev_ts)
        url = f"{GAMMA_API_BASE}/events?slug={slug}"
        session = await self._get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data:
                    return None

                event = data[0]
                metadata = event.get("eventMetadata", {})
                if isinstance(metadata, str):
                    import json as _json
                    try:
                        metadata = _json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        return None

                ptb = metadata.get("priceToBeat")
                final = metadata.get("finalPrice")

                # CRITICAL: priceToBeat(N) = finalPrice(N-1)
                # Only return finalPrice — priceToBeat of prev window is the WRONG value!
                # (priceToBeat = opening price, finalPrice = closing price)
                if final is not None:
                    log.info("Prev window %d resolved: finalPrice=$%.2f (ptb=$%.2f)",
                             prev_ts, float(final), float(ptb) if ptb else 0)
                    return float(final)
                # Not resolved yet — return None, caller will use Binance fallback
                return None
        except Exception as e:
            log.debug("Failed to get previous finalPrice: %s", e)

        return None

    async def get_market_by_window_ts(self, window_ts: int) -> MarketInfo | None:
        slug = build_slug(window_ts)
        url = f"{GAMMA_API_BASE}/events?slug={slug}"

        session = await self._get_session()
        for attempt in range(3):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        log.warning("Gamma API %s returned %d", slug, resp.status)
                        return None
                    data = await resp.json()
            except Exception as e:
                log.warning("Gamma API request failed (attempt %d): %s", attempt + 1, e)
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(2)
                    continue
                return None

            if not data:
                return None

            event = data[0]
            markets = event.get("markets", [])
            if not markets:
                return None

            market = markets[0]
            return self._parse_market(market, event, slug, window_ts)

        return None

    async def get_recent_resolved_markets(self, count: int | None = None) -> list[MarketInfo]:
        count = count or self._config.max_recent_markets
        now_ts = compute_window_timestamp()
        resolved = []

        for i in range(1, count + 1):
            past_ts = now_ts - (i * MARKET_INTERVAL_SECONDS)
            market = await self.get_market_by_window_ts(past_ts)
            if market and market.is_resolved:
                resolved.append(market)

        return resolved

    def seconds_remaining(self, market: MarketInfo) -> float:
        return (market.end_time - datetime.now(timezone.utc)).total_seconds()

    def is_in_entry_window(self, market: MarketInfo) -> bool:
        remaining = self.seconds_remaining(market)
        return 0 < remaining <= self._config.entry_window_seconds

    def _parse_market(
        self, market: dict, event: dict, slug: str, window_ts: int
    ) -> MarketInfo:
        token_ids = json.loads(market.get("clobTokenIds", "[]"))
        up_token = token_ids[0] if len(token_ids) > 0 else ""
        down_token = token_ids[1] if len(token_ids) > 1 else ""

        outcome_prices = json.loads(market.get("outcomePrices", '["0.5","0.5"]'))
        up_price = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0.5
        down_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0.5

        end_date_str = market.get("endDate", "")
        end_time = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

        start_str = event.get("startDate") or event.get("startTime") or ""
        if start_str:
            start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        else:
            start_time = datetime.fromtimestamp(window_ts, tz=timezone.utc)

        resolution_status = market.get("umaResolutionStatus", "")
        is_resolved = resolution_status == "resolved" or market.get("automaticallyResolved", False)

        metadata = {}
        raw_meta = event.get("eventMetadata") or market.get("eventMetadata")
        if isinstance(raw_meta, str):
            try:
                metadata = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(raw_meta, dict):
            metadata = raw_meta

        return MarketInfo(
            condition_id=market.get("conditionId", ""),
            up_token_id=up_token,
            down_token_id=down_token,
            end_time=end_time,
            start_time=start_time,
            slug=slug,
            window_ts=window_ts,
            outcomes=market.get("outcomes", '["Up","Down"]'),
            up_price=up_price,
            down_price=down_price,
            liquidity=float(market.get("liquidityNum", 0)),
            is_closed=bool(market.get("closed", False)),
            is_resolved=is_resolved,
            accepting_orders=bool(market.get("acceptingOrders", True)),
            best_bid=float(market.get("bestBid", 0)),
            best_ask=float(market.get("bestAsk", 0)),
            price_to_beat=metadata.get("priceToBeat"),
        )
