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


@dataclass(frozen=True)
class BotConfig:
    private_key: str
    wallet_address: str
    polygon_rpc_url: str
    position_size_fraction: float
    max_buy_price: float
    abort_price: float
    entry_window_seconds: int
    momentum_lookback_seconds: int
    volatility_threshold: float
    min_market_liquidity: float
    harvest_poll_interval: int
    max_recent_markets: int
    paper_trading: bool = False
    paper_balance: float = 100.0

    @classmethod
    def from_env(cls) -> "BotConfig":
        paper = os.environ.get("PAPER_TRADING", "false").lower() in ("true", "1", "yes")

        private_key = os.environ.get("PRIVATE_KEY", "")
        wallet_address = os.environ.get("WALLET_ADDRESS", "")

        if not paper:
            if not private_key:
                raise ValueError("PRIVATE_KEY is required in .env (or set PAPER_TRADING=true)")
            if not wallet_address:
                raise ValueError("WALLET_ADDRESS is required in .env (or set PAPER_TRADING=true)")

        return cls(
            private_key=private_key or "0x_paper_mode",
            wallet_address=wallet_address or "0x0000000000000000000000000000000000000000",
            polygon_rpc_url=os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com"),
            position_size_fraction=float(os.environ.get("POSITION_SIZE_FRACTION", "0.40")),
            max_buy_price=float(os.environ.get("MAX_BUY_PRICE", "0.83")),
            abort_price=float(os.environ.get("ABORT_PRICE", "0.99")),
            entry_window_seconds=int(os.environ.get("ENTRY_WINDOW_SECONDS", "40")),
            momentum_lookback_seconds=int(os.environ.get("MOMENTUM_LOOKBACK_SECONDS", "30")),
            volatility_threshold=float(os.environ.get("VOLATILITY_THRESHOLD", "0.003")),
            min_market_liquidity=float(os.environ.get("MIN_MARKET_LIQUIDITY", "1000")),
            harvest_poll_interval=int(os.environ.get("HARVEST_POLL_INTERVAL", "30")),
            max_recent_markets=int(os.environ.get("MAX_RECENT_MARKETS_TO_CHECK", "20")),
            paper_trading=paper,
            paper_balance=float(os.environ.get("PAPER_BALANCE", "100.0")),
        )
