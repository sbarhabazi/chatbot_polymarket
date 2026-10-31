from __future__ import annotations

import asyncio
import math

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


def compute_kelly_size(
    confidence: float,
    share_price: float,
    kelly_fraction: float = 0.25,
    fee_rate: float = 0.072,
) -> float:
    """
    Fractional Kelly criterion for optimal position sizing.

    Full Kelly: f* = (p*b - q) / b
      where p = P(win), q = 1-p, b = net payout odds

    We use fractional Kelly (default quarter-Kelly) for safety.
    Quarter-Kelly captures ~75% of growth rate with dramatically lower variance.

    Returns: fraction of bankroll to bet (0.0 if EV is negative).

    Examples at quarter-Kelly:
      conf=0.65, price=$0.55 → 4.7% of bankroll
      conf=0.80, price=$0.55 → 13.4% of bankroll
      conf=0.55, price=$0.55 → 0% (EV negative after fees)
      conf=0.85, price=$0.80 → 5.2% of bankroll
    """
    if confidence <= 0 or confidence >= 1:
        return 0.0
    if share_price <= 0 or share_price >= 1:
        return 0.0

    p = confidence
    q = 1.0 - p

    # Net payout per $1 risked
    profit_per_share = 1.0 - share_price
    fee = fee_rate * profit_per_share
    net_profit = profit_per_share - fee
    b = net_profit / share_price  # Net odds

    if b <= 0:
        return 0.0

    # Full Kelly
    f_full = (p * b - q) / b

    if f_full <= 0:
        return 0.0  # Expected value is negative — don't bet

    # Fractional Kelly
    return f_full * kelly_fraction


POLY_MIN_SHARES = 5  # Polymarket impose un minimum de 5 parts par ordre


def compute_order_shares(
    amount_usdc: float,
    offer_price: float,
    balance: float,
    max_fraction: float,
    min_shares: int = POLY_MIN_SHARES,
) -> int:
    """Nombre entier de parts à acheter, ou 0 si l'ordre doit être sauté.

    Le coût total (parts × prix offert) ne dépasse jamais ``balance × max_fraction``,
    le plafond de risque du mode courant. Si la mise Kelly donne moins que le
    minimum de Polymarket, on ne monte à ``min_shares`` que si ce minimum reste
    sous le plafond ; sinon on saute le trade plutôt que de surmiser.
    """
    if offer_price <= 0 or offer_price >= 1 or balance <= 0 or max_fraction <= 0:
        return 0
    budget = min(amount_usdc, balance * max_fraction)
    # Petite tolérance pour les arrondis en virgule flottante (0.6 / 0.12 = 4.999…)
    size = int(math.floor(budget / offer_price + 1e-9))
    if size >= min_shares:
        return size
    if min_shares * offer_price <= balance * max_fraction + 1e-9:
        return min_shares
    return 0


def compute_offer_price(price: float, best_ask: float, ask_buffer: float,
                        max_buy_price: float) -> float:
    """Prix offert : max(best_ask, prix du marché) + ask_buffer, arrondi au cent
    supérieur et plafonné à max_buy_price."""
    anchor = best_ask if best_ask > price else price
    # round() avant ceil() : (0.40 + 0.02) * 100 = 42.000000000000004 en flottant
    cents = math.ceil(round((anchor + ask_buffer) * 100, 6))
    return min(cents / 100, max_buy_price)


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
                raw = await asyncio.to_thread(usdc_e.functions.balanceOf(wallet).call)
                self._cached_balance = raw / 1e6
            except Exception as e:
                log.error("Failed to fetch on-chain USDC.e balance: %s", e)
                self._cached_balance = self._cached_balance or 0.0
        elif self._client:
            try:
                raw = await asyncio.to_thread(self._client.get_balance_allowance, asset_type=0)
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
            raw = await asyncio.to_thread(usdc_native.functions.balanceOf(wallet).call)
            return raw / 1e6
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
