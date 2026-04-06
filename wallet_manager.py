from web3 import Web3

from config import BotConfig
from contracts.addresses import USDC_E, USDC_NATIVE
from logger import log

BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    }
]


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
    def __init__(self, config: BotConfig, clob_client=None, w3: Web3 | None = None):
        self._config = config
        self._client = clob_client
        self._w3 = w3
        self._cached_balance: float | None = None
        self._paper_balance: float = config.paper_balance

    async def get_usdc_balance(self, force_refresh: bool = False) -> float:
        if self._config.paper_trading:
            return self._paper_balance

        if self._cached_balance is not None and not force_refresh:
            return self._cached_balance

        # Read USDC.e balance on-chain (what Polymarket actually uses)
        if self._w3:
            try:
                wallet = Web3.to_checksum_address(self._config.wallet_address)
                usdc_e = self._w3.eth.contract(
                    address=Web3.to_checksum_address(USDC_E), abi=BALANCE_ABI,
                )
                self._cached_balance = usdc_e.functions.balanceOf(wallet).call() / 1e6
            except Exception as e:
                log.error("Failed to fetch on-chain USDC.e balance: %s", e)
                self._cached_balance = self._cached_balance or 0.0
        elif self._client:
            try:
                raw = self._client.get_balance_allowance(asset_type=0)
                balance_wei = int(raw.get("balance", "0"))
                self._cached_balance = balance_wei / 1e6
            except Exception as e:
                log.error("Failed to fetch CLOB USDC balance: %s", e)
                self._cached_balance = self._cached_balance or 0.0
        else:
            self._cached_balance = 0.0

        return self._cached_balance

    async def get_native_usdc_balance(self) -> float:
        """Check native USDC balance (needs swap to USDC.e for Polymarket)."""
        if not self._w3:
            return 0.0
        try:
            wallet = Web3.to_checksum_address(self._config.wallet_address)
            usdc_native = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_NATIVE), abi=BALANCE_ABI,
            )
            return usdc_native.functions.balanceOf(wallet).call() / 1e6
        except Exception as e:
            log.error("Failed to fetch native USDC balance: %s", e)
            return 0.0

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
