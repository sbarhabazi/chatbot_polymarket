import asyncio
import json
import ssl
import time
import warnings
from collections import deque

import certifi
import numpy as np
import websockets

from config import BINANCE_WS_URL, BotConfig
from logger import log
from models import PriceSignal, PriceSnapshot

warnings.filterwarnings("ignore", message="Polyfit may be poorly conditioned")


class PriceAnalyzer:
    def __init__(self, config: BotConfig):
        self._config = config
        self._prices: deque[PriceSnapshot] = deque(maxlen=1200)
        self._running = False
        self._connected = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._consume_stream())
        log.info("PriceAnalyzer started (Binance WS)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("PriceAnalyzer stopped")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_current_price(self) -> float | None:
        if not self._prices:
            return None
        return self._prices[-1].price

    # ─── WebSocket Consumer ───────────────────────────────────

    async def _consume_stream(self) -> None:
        backoff = 1
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        while self._running:
            try:
                async with websockets.connect(BINANCE_WS_URL, ssl=ssl_ctx) as ws:
                    self._connected = True
                    backoff = 1
                    log.info("Binance WS connected")
                    async for message in ws:
                        if not self._running:
                            break
                        data = json.loads(message)
                        self._prices.append(PriceSnapshot(
                            price=float(data["p"]),
                            timestamp=float(data["T"]) / 1000.0,
                            volume=float(data["q"]),
                            is_buyer_maker=bool(data.get("m", False)),
                        ))
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                log.warning("Binance WS disconnected: %s, reconnect in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self._connected = False

    # ─── Data Access ──────────────────────────────────────────

    def _get_recent(self, lookback_seconds: float) -> list[PriceSnapshot]:
        if not self._prices:
            return []
        cutoff = time.time() - lookback_seconds
        return [p for p in self._prices if p.timestamp >= cutoff]

    # ─── Momentum (Multi-Timeframe) ─────���────────────────────

    def _momentum(self, lookback_seconds: float) -> float:
        recent = self._get_recent(lookback_seconds)
        if len(recent) < 5:
            return 0.0
        times = np.array([p.timestamp for p in recent])
        prices = np.array([p.price for p in recent])
        times_norm = times - times[0]
        if times_norm[-1] == 0:
            return 0.0
        coeffs = np.polyfit(times_norm, prices, 1)
        return float(coeffs[0])

    def compute_multi_timeframe_momentum(self) -> tuple[float, float, float]:
        return self._momentum(10.0), self._momentum(30.0), self._momentum(60.0)

    # ─── Volatility ───────────────────────────────────────────

    def compute_volatility(self, lookback_seconds: float = 30.0) -> float:
        recent = self._get_recent(lookback_seconds)
        if len(recent) < 10:
            return 0.0
        prices = np.array([p.price for p in recent])
        mean_price = np.mean(prices)
        if mean_price == 0:
            return 0.0
        return float(np.std(prices) / mean_price)

    # ─── Volume Analysis ──────────────────────────────────────

    def compute_buy_volume_ratio(self, lookback_seconds: float = 30.0) -> tuple[float, float]:
        """(buy_ratio, total_volume). buy = taker is buyer (m=False)."""
        recent = self._get_recent(lookback_seconds)
        if len(recent) < 5:
            return 0.5, 0.0
        buy_vol = sum(p.volume for p in recent if not p.is_buyer_maker)
        sell_vol = sum(p.volume for p in recent if p.is_buyer_maker)
        total = buy_vol + sell_vol
        if total == 0:
            return 0.5, 0.0
        return buy_vol / total, total

    # ─── Distance Confidence Model ─────────���──────────────────

    # 2D confidence table: rows=distance thresholds, cols=time breakpoints
    # Covers early entry (120s) through late entry (10s)
    _TIME_COLS = [120, 90, 60, 40, 25, 10]
    _DIST_ROWS = [
        # (min_distance, confidence_at_each_time_col)
        (50, [0.72, 0.78, 0.82, 0.88, 0.92, 0.98]),
        (30, [0.65, 0.72, 0.78, 0.82, 0.85, 0.96]),
        (20, [0.58, 0.65, 0.72, 0.76, 0.78, 0.90]),
        (15, [0.50, 0.58, 0.65, 0.70, 0.72, 0.86]),
        (12, [0.45, 0.52, 0.58, 0.64, 0.65, 0.80]),
    ]

    def compute_distance_confidence(
        self, abs_distance: float, seconds_remaining: float, volatility: float,
    ) -> float:
        """
        Probability that BTC stays on the same side of price_to_beat.
        Uses 2D interpolation over distance and time remaining.

        | Distance | 120s | 90s  | 60s  | 40s  | 25s  | 10s  |
        |----------|------|------|------|------|------|------|
        | $50+     | 0.72 | 0.78 | 0.82 | 0.88 | 0.92 | 0.98 |
        | $30-50   | 0.65 | 0.72 | 0.78 | 0.82 | 0.85 | 0.96 |
        | $20-30   | 0.58 | 0.65 | 0.72 | 0.76 | 0.78 | 0.90 |
        | $15-20   | 0.50 | 0.58 | 0.65 | 0.70 | 0.72 | 0.86 |
        | $12-15   | 0.45 | 0.52 | 0.58 | 0.64 | 0.65 | 0.80 |
        | < $12    | SKIP | SKIP | SKIP | SKIP | SKIP | SKIP |
        """
        if abs_distance < 12.0:
            return 0.0

        # Find distance row (first matching row where abs_distance >= threshold)
        row_values = None
        for min_dist, values in self._DIST_ROWS:
            if abs_distance >= min_dist:
                row_values = values
                break
        if row_values is None:
            return 0.0

        # Interpolate along time axis
        t = seconds_remaining
        cols = self._TIME_COLS
        vals = row_values

        # Clamp to table bounds
        if t >= cols[0]:  # >= 120s
            base = vals[0]
        elif t <= cols[-1]:  # <= 10s
            base = vals[-1]
        else:
            # Find bounding columns and interpolate
            for i in range(len(cols) - 1):
                if cols[i] >= t >= cols[i + 1]:
                    frac = (cols[i] - t) / (cols[i] - cols[i + 1])
                    base = vals[i] + frac * (vals[i + 1] - vals[i])
                    break
            else:
                base = vals[0]

        # Volatility penalty
        if volatility >= 0.005:
            base -= 0.08

        return max(0.0, min(0.98, base))

    # ─── Main Signal (Distance-Based) ─────────────────────────

    def compute_distance_signal(
        self, price_to_beat: float, seconds_remaining: float,
    ) -> PriceSignal | None:
        """Compute trading signal based on distance from opening price."""
        current = self.get_current_price()
        if current is None:
            return None

        btc_distance = current - price_to_beat
        abs_distance = abs(btc_distance)
        recommended_side = "Up" if btc_distance > 0 else "Down"

        # Volatility
        volatility = self.compute_volatility(30.0)
        is_extreme = volatility >= self._config.volatility_threshold

        # Distance confidence
        dist_conf = self.compute_distance_confidence(abs_distance, seconds_remaining, volatility)

        # Multi-timeframe momentum
        m10, m30, m60 = self.compute_multi_timeframe_momentum()

        # Count momentum agreement with distance direction
        # Slopes near zero (< $0.5/s) are "neutral" — not opposing
        NEUTRAL_THRESHOLD = 0.5  # $/second
        agreement = 0
        opposing = 0
        for slope in [m10, m30, m60]:
            if abs(slope) < NEUTRAL_THRESHOLD:
                continue  # Neutral, skip
            if (btc_distance > 0 and slope > 0) or (btc_distance < 0 and slope < 0):
                agreement += 1
            else:
                opposing += 1

        # Volume
        bvr, total_vol = self.compute_buy_volume_ratio(30.0)

        # Start with distance confidence
        confidence = dist_conf
        skip_reason = None

        # Skip: distance too small
        if abs_distance < self._config.min_btc_distance:
            skip_reason = f"Distance ${abs_distance:.1f} < min ${self._config.min_btc_distance}"
            confidence = 0.0

        # Skip: extreme volatility
        elif is_extreme:
            skip_reason = f"Extreme volatility ({volatility:.5f})"
            confidence = 0.0

        # Skip: strong momentum reversal (only in last 60s when momentum is meaningful)
        elif opposing == 3 and abs_distance < 30 and seconds_remaining <= 60:
            skip_reason = f"All momentum opposes distance ({opposing}/3 opposing)"
            confidence = 0.0

        else:
            # Penalty: insufficient momentum (only in last 60s)
            if seconds_remaining <= 60 and agreement < self._config.momentum_confirmation_required:
                confidence -= 0.05

            # Penalty: volume contradicts direction
            if recommended_side == "Up" and bvr < 0.35:
                confidence -= 0.03
            elif recommended_side == "Down" and bvr > 0.65:
                confidence -= 0.03

            confidence = max(0.0, confidence)

        return PriceSignal(
            current_price=current,
            price_to_beat=price_to_beat,
            btc_distance=btc_distance,
            seconds_remaining=seconds_remaining,
            distance_confidence=dist_conf,
            momentum_10s=m10,
            momentum_30s=m30,
            momentum_60s=m60,
            momentum_agreement=agreement,
            buy_volume_ratio=bvr,
            total_volume_30s=total_vol,
            volatility_30s=volatility,
            is_extreme_volatility=is_extreme,
            recommended_side=recommended_side,
            confidence=confidence,
            skip_reason=skip_reason,
        )
