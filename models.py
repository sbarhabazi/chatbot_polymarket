from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MarketInfo:
    condition_id: str
    up_token_id: str
    down_token_id: str
    end_time: datetime
    start_time: datetime
    slug: str
    window_ts: int
    outcomes: list[str] = field(default_factory=lambda: ["Up", "Down"])
    up_price: float = 0.5
    down_price: float = 0.5
    liquidity: float = 0.0
    is_closed: bool = False
    is_resolved: bool = False
    accepting_orders: bool = True
    best_bid: float = 0.0
    best_ask: float = 0.0
    price_to_beat: float | None = None


@dataclass
class PriceSnapshot:
    price: float
    timestamp: float
    volume: float
    is_buyer_maker: bool = False  # Binance "m" field: True = seller aggression


@dataclass
class PriceSignal:
    # Core
    current_price: float
    price_to_beat: float
    btc_distance: float           # current - price_to_beat (positive = Up)
    seconds_remaining: float

    # Distance confidence
    distance_confidence: float

    # Multi-timeframe momentum (confirmation)
    momentum_10s: float
    momentum_30s: float
    momentum_60s: float
    momentum_agreement: int       # 0-3 timeframes agreeing with distance direction

    # Volume
    buy_volume_ratio: float       # buy vol / total vol (>0.5 = bullish)
    total_volume_30s: float

    # Volatility
    volatility_30s: float
    is_extreme_volatility: bool

    # Decision
    recommended_side: str         # "Up" or "Down"
    confidence: float             # final confidence after all adjustments
    skip_reason: str | None = None

    # ── Phase 1: New Indicators ──
    obi: float = 0.0              # Order Book Imbalance (-1 to +1)
    rsi: float = 50.0             # Fast RSI (0-100)
    vwap: float | None = None     # Volume-Weighted Average Price
    vwap_deviation: float = 0.0   # % deviation from VWAP
    trade_rate: float = 0.0       # Current trades/second
    trade_rate_ratio: float = 1.0 # Current rate / avg rate
    buy_ratio_5s: float = 0.5    # Multi-TF buy ratio (5s window)
    buy_ratio_15s: float = 0.5   # Multi-TF buy ratio (15s window)
    buy_ratio_60s: float = 0.5   # Multi-TF buy ratio (60s window)
    signal_agreement: int = 0     # How many new signals agree with direction
    signal_total: int = 0         # Total new signals evaluated


@dataclass
class OrderBook:
    """Carnet d'ordres CLOB d'un token : asks triés du moins cher au plus cher,
    bids du plus cher au moins cher. Chaque niveau = (prix, nombre de parts)."""
    asks: list[tuple[float, float]] = field(default_factory=list)
    bids: list[tuple[float, float]] = field(default_factory=list)

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    def fill_cost(self, shares: float, limit_price: float) -> float | None:
        """Coût d'achat de `shares` parts en remontant les asks jusqu'à
        `limit_price` (comme un ordre FOK). None si le carnet ne suffit pas."""
        remaining, cost = shares, 0.0
        for price, size in self.asks:
            if price > limit_price + 1e-9:
                break
            take = min(remaining, size)
            cost += take * price
            remaining -= take
            if remaining <= 1e-9:
                return cost
        return None
