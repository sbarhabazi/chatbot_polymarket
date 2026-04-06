import asyncio
import json
import ssl
import time
from collections import deque

import warnings

import certifi
import numpy as np
import websockets

warnings.filterwarnings("ignore", message="Polyfit may be poorly conditioned")

from config import BINANCE_WS_URL, BotConfig
from logger import log
from models import PriceSignal, PriceSnapshot


class PriceAnalyzer:
    def __init__(self, config: BotConfig):
        self._config = config
        self._prices: deque[PriceSnapshot] = deque(maxlen=600)  # ~5 min of ticks
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
                        ))
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                log.warning("Binance WS disconnected: %s, reconnect in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

        self._connected = False

    def _get_recent_prices(self, lookback_seconds: float) -> list[PriceSnapshot]:
        if not self._prices:
            return []
        cutoff = time.time() - lookback_seconds
        return [p for p in self._prices if p.timestamp >= cutoff]

    def _calculate_momentum(self, lookback_seconds: float) -> tuple[float, float]:
        """Returns (slope in $/s, acceleration)."""
        recent = self._get_recent_prices(lookback_seconds)
        if len(recent) < 10:
            return 0.0, 0.0

        times = np.array([p.timestamp for p in recent])
        prices = np.array([p.price for p in recent])

        # Normalize times to avoid numerical issues
        t0 = times[0]
        times_norm = times - t0

        # Linear regression for momentum (slope)
        coeffs = np.polyfit(times_norm, prices, 1)
        slope = coeffs[0]  # $/second

        # Acceleration: split into halves and compare slopes
        mid = len(recent) // 2
        if mid < 5:
            return slope, 0.0

        first_half = recent[:mid]
        second_half = recent[mid:]

        t1 = np.array([p.timestamp - t0 for p in first_half])
        p1 = np.array([p.price for p in first_half])
        t2 = np.array([p.timestamp - t0 for p in second_half])
        p2 = np.array([p.price for p in second_half])

        slope1 = np.polyfit(t1, p1, 1)[0] if len(t1) >= 3 else 0.0
        slope2 = np.polyfit(t2, p2, 1)[0] if len(t2) >= 3 else 0.0

        duration = times_norm[-1] - times_norm[0]
        acceleration = (slope2 - slope1) / duration if duration > 0 else 0.0

        return slope, acceleration

    def _calculate_volatility(self, lookback_seconds: float) -> float:
        recent = self._get_recent_prices(lookback_seconds)
        if len(recent) < 10:
            return 0.0
        prices = np.array([p.price for p in recent])
        mean_price = np.mean(prices)
        if mean_price == 0:
            return 0.0
        return float(np.std(prices) / mean_price)

    def detect_resistance_wall(self) -> bool:
        """Detect 3+ price reversals at the same level (0.05% tolerance)."""
        lookback = self._config.momentum_lookback_seconds * 2
        recent = self._get_recent_prices(lookback)
        if len(recent) < 30:
            return False

        prices = [p.price for p in recent]
        reversals: list[float] = []

        for i in range(1, len(prices) - 1):
            if (prices[i] > prices[i - 1] and prices[i] > prices[i + 1]) or \
               (prices[i] < prices[i - 1] and prices[i] < prices[i + 1]):
                reversals.append(prices[i])

        if len(reversals) < 3:
            return False

        # Group reversals by proximity (0.05% tolerance)
        tolerance = np.mean(prices) * 0.0005
        for i, r1 in enumerate(reversals):
            cluster = sum(1 for r2 in reversals if abs(r1 - r2) <= tolerance)
            if cluster >= 3:
                return True

        return False

    def compute_signal(self) -> PriceSignal | None:
        current = self.get_current_price()
        if current is None:
            return None

        lookback = float(self._config.momentum_lookback_seconds)
        recent = self._get_recent_prices(lookback)
        if len(recent) < 10:
            return None

        momentum, acceleration = self._calculate_momentum(lookback)
        volatility = self._calculate_volatility(lookback)
        is_extreme = volatility > self._config.volatility_threshold
        is_wall = self.detect_resistance_wall()

        # Direction
        if abs(momentum) < 0.01:
            direction = "flat"
        elif momentum > 0:
            direction = "up"
        else:
            direction = "down"

        # Recommended side
        if direction == "up":
            recommended = "Up"
        elif direction == "down":
            recommended = "Down"
        else:
            # Flat: look at acceleration for a tiebreaker
            recommended = "Up" if acceleration >= 0 else "Down"

        # Confidence: higher momentum + lower volatility = higher confidence
        abs_momentum = abs(momentum)
        confidence = min(1.0, abs_momentum / 5.0) * (1.0 - min(1.0, volatility / 0.01))
        confidence = max(0.0, confidence)

        return PriceSignal(
            current_price=current,
            momentum=momentum,
            acceleration=acceleration,
            volatility_30s=volatility,
            price_direction=direction,
            is_extreme_volatility=is_extreme,
            is_resistance_wall=is_wall,
            confidence=confidence,
            recommended_side=recommended,
        )
