import asyncio

from web3 import Web3

from config import BotConfig
from contracts.abis import BALANCE_OF_ABI, REDEEM_POSITIONS_ABI
from contracts.addresses import CTF, USDC_E
from learning_engine import LearningEngine
from logger import log
from market_manager import MarketManager
from models import MarketInfo
from wallet_manager import WalletManager

ZERO_BYTES32 = b"\x00" * 32
INDEX_SETS = [1, 2]  # Covers both outcomes; only winning one yields payout


class Harvester:
    """Process B: Check resolved markets and auto-claim winning positions."""

    def __init__(
        self,
        config: BotConfig,
        market_manager: MarketManager,
        wallet_manager: WalletManager,
        w3: Web3,
        learning_engine: LearningEngine | None = None,
    ):
        self._config = config
        self._mm = market_manager
        self._wm = wallet_manager
        self._w3 = w3
        self._redeemed: set[str] = set()
        self._le = learning_engine

        self._ctf_contract = w3.eth.contract(
            address=Web3.to_checksum_address(CTF),
            abi=REDEEM_POSITIONS_ABI + BALANCE_OF_ABI,
        )

    async def run(self) -> None:
        log.info("Harvester (Process B) started")
        while True:
            try:
                await self._harvest_cycle()
            except Exception as e:
                log.error("Harvester cycle error: %s", e, exc_info=True)
            await asyncio.sleep(self._config.harvest_poll_interval)

    async def _harvest_cycle(self) -> None:
        resolved_markets = await self._mm.get_recent_resolved_markets()

        for market in resolved_markets:
            if market.condition_id in self._redeemed:
                continue

            has_tokens = await self._check_token_balance(market)
            if not has_tokens:
                # No tokens held for this market — mark as done
                self._redeemed.add(market.condition_id)
                continue

            log.info(
                "Found redeemable position: window %d | condition %s...",
                market.window_ts, market.condition_id[:16],
            )

            # Récupérer le solde AVANT redeem pour calculer le payout
            balance_before = await self._wm.get_usdc_balance(force_refresh=True)

            success = await self._redeem_position(market)
            if success:
                self._redeemed.add(market.condition_id)
                self._wm.invalidate_cache()
                balance_after = await self._wm.get_usdc_balance(force_refresh=True)
                payout = max(0.0, balance_after - balance_before)
                log.info("Redeemed successfully: window %d | payout=$%.2f",
                         market.window_ts, payout)

                # Enregistrer le résultat dans le learning engine
                if self._le:
                    won = payout > 0
                    self._le.record_result(market.window_ts, won=won, payout=payout)
                    log.info("LearningEngine updated: %s", self._le.summary())
            else:
                log.warning("Redemption failed for window %d, will retry", market.window_ts)

    async def _check_token_balance(self, market: MarketInfo) -> bool:
        wallet = Web3.to_checksum_address(self._config.wallet_address)

        for token_id_str in [market.up_token_id, market.down_token_id]:
            if not token_id_str:
                continue
            try:
                balance = self._ctf_contract.functions.balanceOf(
                    wallet, int(token_id_str)
                ).call()
                if balance > 0:
                    return True
            except Exception as e:
                log.debug("balanceOf check failed for token %s...: %s", token_id_str[:12], e)

        return False

    async def _redeem_position(self, market: MarketInfo) -> bool:
        wallet = Web3.to_checksum_address(self._config.wallet_address)
        condition_bytes = bytes.fromhex(
            market.condition_id[2:] if market.condition_id.startswith("0x") else market.condition_id
        )

        # Retry up to 3 times with increasing gas
        for attempt in range(1, 4):
            try:
                nonce = self._w3.eth.get_transaction_count(wallet, "pending")
                gas_multiplier = 1.5 + (attempt * 0.5)  # 2x, 2.5x, 3x
                gas_price = int(self._w3.eth.gas_price * gas_multiplier)

                tx = self._ctf_contract.functions.redeemPositions(
                    Web3.to_checksum_address(USDC_E),
                    ZERO_BYTES32,
                    condition_bytes,
                    INDEX_SETS,
                ).build_transaction({
                    "chainId": 137,
                    "from": wallet,
                    "nonce": nonce,
                    "gasPrice": gas_price,
                    "gas": 250_000,
                })

                signed = self._w3.eth.account.sign_transaction(tx, self._config.private_key)
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                log.info("Redeem tx sent (attempt %d, gas %.1fx): %s", attempt, gas_multiplier, tx_hash.hex())

                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
                if receipt["status"] == 1:
                    log.info("Redeem CONFIRMED: %s (gas used: %d)", tx_hash.hex(), receipt["gasUsed"])
                    return True
                else:
                    log.error("Redeem tx reverted: %s", tx_hash.hex())
                    return False

            except Exception as e:
                log.warning("Redeem attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    import asyncio
                    await asyncio.sleep(5)

        log.error("Redeem failed after 3 attempts for window %d", market.window_ts)
        return False
