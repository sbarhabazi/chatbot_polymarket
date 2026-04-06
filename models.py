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
