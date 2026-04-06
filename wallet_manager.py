from config import BotConfig
from logger import log


def calculate_gross_yield(share_price: float) -> float:
    if share_price <= 0 or share_price >= 1:
        return 0.0
    return (1.0 - share_price) / share_price


def calculate_net_yield(share_price: float, fee_rate: float = 0.072) -> float:
    if share_price <= 0 or share_price >= 1:
        return 0.0
    profit = 1.0 - share_price
    fee = fee_rate * profit
    return (profit - fee) / share_price


class WalletManager:
    def __init__(self, config: BotConfig, clob_client=None):
        self._config = config
        self._client = clob_client
        self._cached_balance: float | None = None
        self._paper_balance: float = config.paper_balance

    async def get_usdc_balance(self, force_refresh: bool = False) -> float:
        if self._config.paper_trading:
            return self._paper_balance

        if self._cached_balance is not None and not force_refresh:
            return self._cached_balance

        try:
            raw = self._client.get_balance_allowance(asset_type=0)
            balance_wei = int(raw.get("balance", "0"))
            self._cached_balance = balance_wei / 1e6
        except Exception as e:
            log.error("Failed to fetch USDC balance: %s", e)
            if self._cached_balance is None:
                self._cached_balance = 0.0

        return self._cached_balance

    def calculate_bet_amount(self, balance: float) -> float:
        amount = balance * self._config.position_size_fraction
        if amount < 5.0:
            return 0.0
        return round(amount, 2)

    @staticmethod
    def yield_acceptable(price: float, max_price: float = 0.83) -> bool:
        return 0 < price <= max_price

    @staticmethod
    def yield_too_low(price: float, abort_price: float = 0.99) -> bool:
        return price >= abort_price

    def invalidate_cache(self) -> None:
        self._cached_balance = None

    def paper_deduct(self, amount: float) -> None:
        self._paper_balance = max(0, self._paper_balance - amount)

    def paper_add(self, amount: float) -> None:
        self._paper_balance += amount
