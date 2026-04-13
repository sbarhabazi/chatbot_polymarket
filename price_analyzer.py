from __future__ import annotations

"""
Price Analyzer v5 — Composite Signal Engine

Signals used (ranked by predictive power):
  1. Distance confidence  (2D table: distance x time remaining)
  2. Order Book Imbalance  (Binance depth stream, top 10 levels)
  3. Multi-TF momentum     (linear regression 10s/30s/60s)
  4. Multi-TF volume ratio (taker buy ratio at 5s/15s/30s/60s)
  5. VWAP deviation        (current price vs volume-weighted average)
  6. RSI (fast)            (14-period on 0.5s sampled ticks)
  7. Trade arrival rate    (volume spike detection)
  8. Graduated volatility  (penalty scales with vol level)
"""

import asyncio
import json
import ssl
import time
import warnings
from collections import deque

import certifi
import numpy as np
import websockets

from config import BINANCE_WS_URL, BINANCE_DEPTH_WS_URL, BotConfig
from logger import log
from models import PriceSignal, PriceSnapshot

warnings.filterwarnings("ignore", message="Polyfit may be poorly conditioned")


class PriceAnalyzer:
    def __init__(self, config: BotConfig):
        self._config = config
        self._prices: deque[PriceSnapshot] = deque(maxlen=1200)
        self._running = False
        self._connected = False
        self._depth_connected = False
        self._task: asyncio.Task | None = None
        self._depth_task: asyncio.Task | None = None

        # Order book state (latest snapshot from depth stream)
        self._bids: list[list[float]] = []   # [[price, qty], ...]
        self._asks: list[list[float]] = []
        self._depth_update_time: float = 0.0

    # ─── Lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._consume_trade_stream())
        self._depth_task = asyncio.create_task(self._consume_depth_stream())
        log.info("PriceAnalyzer v5 started (Trade + Depth streams)")

    async def stop(self) -> None:
        self._running = False
        for task in [self._task, self._depth_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info("PriceAnalyzer stopped")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_depth_connected(self) -> bool:
        return self._depth_connected

    def get_current_price(self) -> float | None:
        if not self._prices:
            return None
        return self._prices[-1].price

    # ─── Trade WebSocket Consumer ─────────────────────────────

    async def _consume_trade_stream(self) -> None:
        backoff = 1
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        while self._running:
            try:
                async with websockets.connect(BINANCE_WS_URL, ssl=ssl_ctx) as ws:
                    self._connected = True
                    backoff = 1
                    log.info("Binance Trade WS connected")
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
                log.warning("Trade WS disconnected: %s, reconnect in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self._connected = False

    # ─── Depth WebSocket Consumer (NEW) ───────────────────────

    async def _consume_depth_stream(self) -> None:
        backoff = 1
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        while self._running:
            try:
                async with websockets.connect(BINANCE_DEPTH_WS_URL, ssl=ssl_ctx) as ws:
                    self._depth_connected = True
                    backoff = 1
                    log.info("Binance Depth WS connected (top 20 levels, 100ms)")
                    async for message in ws:
                        if not self._running:
                            break
                        data = json.loads(message)
                        self._bids = [
                            [float(b[0]), float(b[1])]
                            for b in data.get("bids", [])
                        ]
                        self._asks = [
                            [float(a[0]), float(a[1])]
                            for a in data.get("asks", [])
                        ]
                        self._depth_update_time = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._depth_connected = False
                log.warning("Depth WS disconnected: %s, reconnect in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self._depth_connected = False

    # ─── Data Access ──────────────────────────────────────────

    def _get_recent(self, lookback_seconds: float) -> list[PriceSnapshot]:
        if not self._prices:
            return []
        cutoff = time.time() - lookback_seconds
        return [p for p in self._prices if p.timestamp >= cutoff]

    # ═══════════════════════════════════════════════════════════
    #  INDICATOR 1: Order Book Imbalance (OBI)
    # ═══════════════════════════════════════════════════════════

    def compute_obi(self, levels: int = 10) -> float:
        """
        Order Book Imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        Range: -1 (pure sell pressure) to +1 (pure buy pressure)
        Returns 0.0 if depth data is stale (>2s) or unavailable.
        """
        if not self._bids or not self._asks:
            return 0.0
        if time.time() - self._depth_update_time > 2.0:
            return 0.0

        bid_vol = sum(b[1] for b in self._bids[:levels])
        ask_vol = sum(a[1] for a in self._asks[:levels])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    # ═══════════════════════════════════════════════════════════
    #  INDICATOR 2: VWAP (Volume-Weighted Average Price)
    # ═══════════════════════════════════════════════════════════

    def compute_vwap(self, lookback_seconds: float = 60.0) -> float | None:
        """VWAP over last N seconds. Returns None if insufficient data."""
        recent = self._get_recent(lookback_seconds)
        if len(recent) < 10:
            return None
        total_vp = sum(p.price * p.volume for p in recent)
        total_v = sum(p.volume for p in recent)
        if total_v == 0:
            return None
        return total_vp / total_v

    # ═══════════════════════════════════════════════════════════
    #  INDICATOR 3: RSI (Fast, tick-based)
    # ═══════════════════════════════════════════════════════════

    def compute_rsi(self, lookback_seconds: float = 30.0, period: int = 14) -> float:
        """
        RSI on 0.5s-sampled tick data.
        Returns 50.0 (neutral) if insufficient data.
        """
        recent = self._get_recent(lookback_seconds)
        if len(recent) < period + 1:
            return 50.0

        # Sample at ~0.5s intervals to reduce noise
        prices = []
        last_t = 0.0
        for p in recent:
            if p.timestamp - last_t >= 0.5:
                prices.append(p.price)
                last_t = p.timestamp

        if len(prices) < period + 1:
            return 50.0

        arr = np.array(prices[-(period + 1):])
        deltas = np.diff(arr)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = float(np.mean(gains))
        avg_loss = float(np.mean(losses))

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0

        rs = avg_gain / avg_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    # ═══════════════════════════════════════════════════════════
    #  INDICATOR 4: Trade Arrival Rate
    # ═══════════════════════════════════════════════════════════

    def compute_trade_arrival_rate(self) -> tuple[float, float]:
        """
        Returns (current_rate, avg_rate) in trades/second.
        current_rate: last 5 seconds
        avg_rate: last 60 seconds
        """
        recent_5s = self._get_recent(5.0)
        recent_60s = self._get_recent(60.0)

        current_rate = len(recent_5s) / 5.0 if recent_5s else 0.0
        avg_rate = len(recent_60s) / 60.0 if recent_60s else 0.0

        return current_rate, avg_rate

    # ═══════════════════════════════════════════════════════════
    #  INDICATOR 5: Multi-Timeframe Buy Volume Ratio
    # ═══════════════════════════════════════════════════════════

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

    def compute_multi_tf_buy_ratio(self) -> dict[str, float]:
        """Buy volume ratios across 4 timeframes."""
        result = {}
        for label, seconds in [("5s", 5.0), ("15s", 15.0), ("30s", 30.0), ("60s", 60.0)]:
            ratio, _ = self.compute_buy_volume_ratio(seconds)
            result[label] = ratio
        return result

    # ═══════════════════════════════════════════════════════════
    #  Momentum (Multi-Timeframe)
    # ═══════════════════════════════════════════════════════════

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

    # ═══════════════════════════════════════════════════════════
    #  Volatility
    # ═══════════════════════════════════════════════════════════

    def compute_volatility(self, lookback_seconds: float = 30.0) -> float:
        recent = self._get_recent(lookback_seconds)
        if len(recent) < 10:
            return 0.0
        prices = np.array([p.price for p in recent])
        mean_price = np.mean(prices)
        if mean_price == 0:
            return 0.0
        return float(np.std(prices) / mean_price)

    # ═══════════════════════════════════════════════════════════
    #  Distance Confidence Model (2D Table — unchanged)
    # ═══════════════════════════════════════════════════════════

    _TIME_COLS = [120, 90, 60, 40, 25, 10]
    _DIST_ROWS = [
        (50, [0.72, 0.78, 0.82, 0.88, 0.92, 0.98]),
        (30, [0.65, 0.72, 0.78, 0.82, 0.85, 0.96]),
        (20, [0.58, 0.65, 0.72, 0.76, 0.78, 0.90]),
        (15, [0.50, 0.58, 0.65, 0.70, 0.72, 0.86]),
        (12, [0.45, 0.52, 0.58, 0.64, 0.65, 0.80]),
    ]

    def compute_distance_confidence(
        self, abs_distance: float, seconds_remaining: float,
    ) -> float:
        """Base confidence from 2D table (no volatility penalty here — moved to composite)."""
        if abs_distance < 12.0:
            return 0.0

        row_values = None
        for min_dist, values in self._DIST_ROWS:
            if abs_distance >= min_dist:
                row_values = values
                break
        if row_values is None:
            return 0.0

        t = seconds_remaining
        cols = self._TIME_COLS
        vals = row_values

        if t >= cols[0]:
            base = vals[0]
        elif t <= cols[-1]:
            base = vals[-1]
        else:
            for i in range(len(cols) - 1):
                if cols[i] >= t >= cols[i + 1]:
                    frac = (cols[i] - t) / (cols[i] - cols[i + 1])
                    base = vals[i] + frac * (vals[i + 1] - vals[i])
                    break
            else:
                base = vals[0]

        return max(0.0, min(0.98, base))

    # ═══════════════════════════════════════════════════════════
    #  COMPOSITE SIGNAL (v5 — all indicators combined)
    # ═══════════════════════════════════════════════════════════

    def compute_distance_signal(
        self, price_to_beat: float, seconds_remaining: float,
    ) -> PriceSignal | None:
        """
        Composite signal combining 7 indicators:

        Base:  Distance confidence from 2D table

        Boosts / Penalties:
          1. OBI agreement:          +0.08 / -0.10
          2. Multi-TF momentum:      +0.05 / -0.08 (skip if all oppose)
          3. Volume ratio accel:     +0.04 / -0.04
          4. RSI confirmation:       +0.03 / -0.04
          5. VWAP mean-reversion:    +0.03 / -0.03
          6. Trade rate spike:       +0.03 / -0.03
          7. Graduated volatility:   0 / -0.03 / -0.06 / SKIP
        """
        current = self.get_current_price()
        if current is None:
            return None

        btc_distance = current - price_to_beat
        abs_distance = abs(btc_distance)
        recommended_side = "Up" if btc_distance > 0 else "Down"
        is_up = btc_distance > 0

        # ── Compute all metrics ──
        volatility = self.compute_volatility(30.0)
        dist_conf = self.compute_distance_confidence(abs_distance, seconds_remaining)
        m10, m30, m60 = self.compute_multi_timeframe_momentum()
        bvr_30, total_vol = self.compute_buy_volume_ratio(30.0)

        # New indicators
        obi = self.compute_obi(levels=10)
        vwap = self.compute_vwap(60.0)
        rsi = self.compute_rsi(30.0, period=14)
        trade_rate_current, trade_rate_avg = self.compute_trade_arrival_rate()
        trade_rate_ratio = (
            trade_rate_current / trade_rate_avg if trade_rate_avg > 0 else 1.0
        )
        multi_tf_bvr = self.compute_multi_tf_buy_ratio()

        # VWAP deviation (%)
        vwap_deviation = 0.0
        if vwap and vwap > 0:
            vwap_deviation = (current - vwap) / vwap * 100

        # ── Build composite confidence ──
        confidence = dist_conf
        skip_reason = None
        signal_agreement = 0
        signal_total = 0
        mom_agreement = 0

        # ── Hard Skips ──
        if abs_distance < self._config.min_btc_distance:
            skip_reason = f"Distance ${abs_distance:.1f} < min ${self._config.min_btc_distance}"
            confidence = 0.0

        elif volatility >= 0.008:
            skip_reason = f"Extreme volatility ({volatility:.5f} >= 0.008)"
            confidence = 0.0

        else:
            # ────────────────────────────────────────────────
            # 1. OBI Signal  (+0.08 / -0.10)
            # ────────────────────────────────────────────────
            obi_threshold = 0.05
            if (is_up and obi > obi_threshold) or (not is_up and obi < -obi_threshold):
                # OBI agrees with direction
                obi_boost = min(0.08, abs(obi) * 0.4)
                confidence += obi_boost
                signal_agreement += 1
            elif (is_up and obi < -obi_threshold) or (not is_up and obi > obi_threshold):
                # OBI opposes direction
                obi_penalty = min(0.10, abs(obi) * 0.5)
                confidence -= obi_penalty
            signal_total += 1

            # ────────────────────────────────────────────────
            # 2. Multi-TF Momentum  (+0.05 / -0.08 / SKIP)
            # ────────────────────────────────────────────────
            NEUTRAL_THRESHOLD = 0.5  # $/second
            opposing = 0
            for slope in [m10, m30, m60]:
                if abs(slope) < NEUTRAL_THRESHOLD:
                    continue
                if (is_up and slope > 0) or (not is_up and slope < 0):
                    mom_agreement += 1
                else:
                    opposing += 1

            if mom_agreement >= 2:
                confidence += 0.05
                signal_agreement += 1
            elif opposing >= 2 and seconds_remaining <= 60:
                # Only penalize momentum opposition late in window
                # At 90-120s, momentum isn't yet meaningful
                confidence -= 0.08

            # Hard skip: all 3 oppose + small distance + late in window
            if opposing == 3 and abs_distance < 30 and seconds_remaining <= 60:
                skip_reason = f"All momentum opposes ({opposing}/3)"
                confidence = 0.0
            signal_total += 1

            # ────────────────────────────────────────────────
            # 3. Multi-TF Volume Acceleration  (+0.04 / -0.04)
            # ────────────────────────────────────────────────
            if not skip_reason:
                bvr_5 = multi_tf_bvr["5s"]
                bvr_15 = multi_tf_bvr["15s"]
                bvr_60 = multi_tf_bvr["60s"]

                if is_up:
                    if bvr_5 > bvr_15 > bvr_60 and bvr_5 > 0.55:
                        confidence += 0.04
                        signal_agreement += 1
                    elif bvr_5 < bvr_15 < bvr_60 and bvr_5 < 0.45:
                        confidence -= 0.04
                    elif bvr_30 < 0.35:
                        confidence -= 0.03
                else:
                    if bvr_5 < bvr_15 < bvr_60 and bvr_5 < 0.45:
                        confidence += 0.04
                        signal_agreement += 1
                    elif bvr_5 > bvr_15 > bvr_60 and bvr_5 > 0.55:
                        confidence -= 0.04
                    elif bvr_30 > 0.65:
                        confidence -= 0.03
                signal_total += 1

            # ────────────────────────────────────────────────
            # 4. RSI Confirmation  (+0.03 / -0.04)
            # ────────────────────────────────────────────────
            if not skip_reason:
                if is_up:
                    if 55 <= rsi <= 75:
                        confidence += 0.03
                        signal_agreement += 1
                    elif rsi > 80:
                        confidence -= 0.04  # Overbought, reversal risk
                    elif rsi < 35:
                        confidence -= 0.04  # Dropping, contradicts Up
                else:
                    if 25 <= rsi <= 45:
                        confidence += 0.03
                        signal_agreement += 1
                    elif rsi < 20:
                        confidence -= 0.04  # Oversold, bounce risk
                    elif rsi > 65:
                        confidence -= 0.04  # Rising, contradicts Down
                signal_total += 1

            # ────────────────────────────────────────────────
            # 5. VWAP Mean-Reversion  (+0.03 / -0.03)
            # ────────────────────────────────────────────────
            if not skip_reason and vwap:
                vwap_thresh = 0.01  # 0.01% deviation threshold
                if is_up and vwap_deviation > vwap_thresh:
                    confidence += 0.03  # Price above VWAP = buying pressure
                    signal_agreement += 1
                elif is_up and vwap_deviation < -vwap_thresh:
                    confidence -= 0.03  # Price below VWAP contradicts Up
                elif not is_up and vwap_deviation < -vwap_thresh:
                    confidence += 0.03  # Price below VWAP = selling pressure
                    signal_agreement += 1
                elif not is_up and vwap_deviation > vwap_thresh:
                    confidence -= 0.03  # Price above VWAP contradicts Down
                signal_total += 1

            # ────────────────────────────────────────────────
            # 6. Trade Rate Spike  (+0.03 / -0.03)
            # ────────────────────────────────────────────────
            if not skip_reason and trade_rate_avg > 1:
                if trade_rate_ratio > 2.0:  # Volume spike
                    recent_3s = self._get_recent(3.0)
                    if recent_3s:
                        spike_buy_ratio = (
                            sum(1 for p in recent_3s if not p.is_buyer_maker)
                            / len(recent_3s)
                        )
                        if (is_up and spike_buy_ratio > 0.6) or (
                            not is_up and spike_buy_ratio < 0.4
                        ):
                            confidence += 0.03
                            signal_agreement += 1
                        elif (is_up and spike_buy_ratio < 0.3) or (
                            not is_up and spike_buy_ratio > 0.7
                        ):
                            confidence -= 0.03
                    signal_total += 1

            # ────────────────────────────────────────────────
            # 7. Graduated Volatility Penalty
            # ────────────────────────────────────────────────
            if not skip_reason:
                if volatility >= 0.005:
                    confidence -= 0.06
                elif volatility >= 0.003:
                    confidence -= 0.03

            confidence = max(0.0, min(0.98, confidence))

        return PriceSignal(
            # Core
            current_price=current,
            price_to_beat=price_to_beat,
            btc_distance=btc_distance,
            seconds_remaining=seconds_remaining,
            # Distance
            distance_confidence=dist_conf,
            # Momentum
            momentum_10s=m10,
            momentum_30s=m30,
            momentum_60s=m60,
            momentum_agreement=mom_agreement,
            # Volume
            buy_volume_ratio=bvr_30,
            total_volume_30s=total_vol,
            # Volatility
            volatility_30s=volatility,
            is_extreme_volatility=volatility >= 0.008,
            # Decision
            recommended_side=recommended_side,
            confidence=confidence,
            skip_reason=skip_reason,
            # ── New Phase 1 indicators ──
            obi=obi,
            rsi=rsi,
            vwap=vwap,
            vwap_deviation=vwap_deviation,
            trade_rate=trade_rate_current,
            trade_rate_ratio=trade_rate_ratio,
            buy_ratio_5s=multi_tf_bvr["5s"],
            buy_ratio_15s=multi_tf_bvr["15s"],
            buy_ratio_60s=multi_tf_bvr["60s"],
            signal_agreement=signal_agreement,
            signal_total=signal_total,
        )
