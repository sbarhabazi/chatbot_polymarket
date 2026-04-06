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


@dataclass
class PriceSignal:
    current_price: float
    momentum: float
    acceleration: float
    volatility_30s: float
    price_direction: str  # "up" | "down" | "flat"
    is_extreme_volatility: bool
    is_resistance_wall: bool
    confidence: float
    recommended_side: str  # "Up" or "Down"
