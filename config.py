from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
SIGNATURE_TYPE = 0
MARKET_INTERVAL_SECONDS = 300
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_DEPTH_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@depth20@100ms"


@dataclass(frozen=True)
class BotConfig:
    private_key: str
    wallet_address: str
    polygon_rpc_url: str

    # Entry
    entry_window_seconds: int
    max_buy_price: float
    abort_price: float
    min_market_liquidity: float

    # Distance strategy
    min_btc_distance: float
    volatility_threshold: float
    momentum_confirmation_required: int

    # Confidence-based sizing
    high_confidence_threshold: float
    medium_confidence_threshold: float
    high_confidence_size: float
    medium_confidence_size: float
    position_size_fraction: float  # fallback

    # Order execution
    fill_buffer: float
    order_type: str  # "FOK", "GTC", "FAK"

    # Kelly sizing
    kelly_fraction: float
    max_kelly_size: float
    min_bet_amount: float

    # Harvester
    harvest_poll_interval: int
    max_recent_markets: int

    # Paper trading
    paper_trading: bool = False
    paper_balance: float = 100.0

    @classmethod
    def from_env(cls) -> "BotConfig":
        paper = os.environ.get("PAPER_TRADING", "false").lower() in ("true", "1", "yes")

        private_key = os.environ.get("PRIVATE_KEY", "")
        wallet_address = os.environ.get("WALLET_ADDRESS", "")

        if not paper:
            if not private_key:
                raise ValueError("PRIVATE_KEY required (or set PAPER_TRADING=true)")
            if not wallet_address:
                raise ValueError("WALLET_ADDRESS required (or set PAPER_TRADING=true)")

        return cls(
            private_key=private_key or "0x_paper_mode",
            wallet_address=wallet_address or "0x0000000000000000000000000000000000000000",
            polygon_rpc_url=os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"),
            entry_window_seconds=int(os.environ.get("ENTRY_WINDOW_SECONDS", "120")),
            max_buy_price=float(os.environ.get("MAX_BUY_PRICE", "0.90")),
            abort_price=float(os.environ.get("ABORT_PRICE", "0.99")),
            min_market_liquidity=float(os.environ.get("MIN_MARKET_LIQUIDITY", "1000")),
            min_btc_distance=float(os.environ.get("MIN_BTC_DISTANCE", "12.0")),
            volatility_threshold=float(os.environ.get("VOLATILITY_THRESHOLD", "0.005")),
            momentum_confirmation_required=int(os.environ.get("MOMENTUM_CONFIRMATION_REQUIRED", "0")),
            high_confidence_threshold=float(os.environ.get("HIGH_CONFIDENCE_THRESHOLD", "0.72")),
            medium_confidence_threshold=float(os.environ.get("MEDIUM_CONFIDENCE_THRESHOLD", "0.60")),
            high_confidence_size=float(os.environ.get("HIGH_CONFIDENCE_SIZE", "0.30")),
            medium_confidence_size=float(os.environ.get("MEDIUM_CONFIDENCE_SIZE", "0.20")),
            position_size_fraction=float(os.environ.get("POSITION_SIZE_FRACTION", "0.40")),
            fill_buffer=float(os.environ.get("FILL_BUFFER", "0.10")),
            order_type=os.environ.get("ORDER_TYPE", "FOK").upper(),
            kelly_fraction=float(os.environ.get("KELLY_FRACTION", "0.50")),
            max_kelly_size=float(os.environ.get("MAX_KELLY_SIZE", "0.20")),
            min_bet_amount=float(os.environ.get("MIN_BET_AMOUNT", "5.0")),
            harvest_poll_interval=int(os.environ.get("HARVEST_POLL_INTERVAL", "30")),
            max_recent_markets=int(os.environ.get("MAX_RECENT_MARKETS_TO_CHECK", "20")),
            paper_trading=paper,
            paper_balance=float(os.environ.get("PAPER_BALANCE", "100.0")),
        )
