import asyncio

from web3 import Web3

from config import BotConfig
from contracts.abis import BALANCE_OF_ABI, PAYOUT_ABI, REDEEM_POSITIONS_ABI
from contracts.addresses import CTF, USDC_E
from learning_engine import LearningEngine
from logger import log
from market_manager import MarketManager
from models import MarketInfo
from tx_lock import WALLET_TX_LOCK
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
            abi=REDEEM_POSITIONS_ABI + BALANCE_OF_ABI + PAYOUT_ABI,
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

            holdings = await self._read_holdings(market)
            if holdings is None:
                # Lecture on-chain impossible : réessayer au prochain cycle
                continue
            if sum(holdings) == 0:
                # No tokens held for this market — mark as done
                self._redeemed.add(market.condition_id)
                # Un trade encore « en attente » sans aucun token détenu après la
                # résolution n'a jamais été rempli (GTC annulé, réponse ambiguë…) :
                # on le retire pour ne pas fausser l'historique d'apprentissage.
                if self._le and self._le.has_pending_trade(market.window_ts):
                    self._le.discard_pending_trade(market.window_ts)
                continue

            # Gain calculé on-chain (tokens détenus × part payée par issue) plutôt que
            # par différence de solde, faussée si un ordre se remplit pendant le redeem.
            payout = await self._compute_payout(market, holdings)
            if payout is None:
                log.debug("Window %d: condition pas encore réglée on-chain, retry",
                          market.window_ts)
                continue

            log.info(
                "Found redeemable position: window %d | condition %s... | payout=$%.2f",
                market.window_ts, market.condition_id[:16], payout,
            )

            success = await self._redeem_position(market)
            if success:
                self._redeemed.add(market.condition_id)
                self._wm.invalidate_cache()
                log.info("Redeemed successfully: window %d | payout=$%.2f",
                         market.window_ts, payout)

                # Enregistrer le résultat dans le learning engine
                if self._le:
                    won = payout > 0
                    self._le.record_result(market.window_ts, won=won, payout=payout)
                    log.info("LearningEngine updated: %s", self._le.summary())
            else:
                log.warning("Redemption failed for window %d, will retry", market.window_ts)

    @staticmethod
    def _condition_bytes(market: MarketInfo) -> bytes:
        cid = market.condition_id
        return bytes.fromhex(cid[2:] if cid.startswith("0x") else cid)

    async def _read_holdings(self, market: MarketInfo) -> tuple[int, int] | None:
        """Soldes bruts (6 décimales) des tokens Up et Down détenus,
        ou None si une lecture a échoué (on ne conclut rien dans ce cas)."""
        wallet = Web3.to_checksum_address(self._config.wallet_address)
        balances = []
        for token_id_str in [market.up_token_id, market.down_token_id]:
            if not token_id_str:
                balances.append(0)
                continue
            try:
                balances.append(await asyncio.to_thread(
                    self._ctf_contract.functions.balanceOf(wallet, int(token_id_str)).call
                ))
            except Exception as e:
                log.warning("balanceOf check failed for token %s...: %s", token_id_str[:12], e)
                return None
        return balances[0], balances[1]

    async def _compute_payout(self, market: MarketInfo, holdings: tuple[int, int]) -> float | None:
        """USDC.e reçus au redeem : somme des tokens détenus × payout de leur issue.

        L'issue 0 est Up et l'issue 1 Down (même ordre que clobTokenIds).
        None si la condition n'est pas encore réglée on-chain ou si la lecture échoue.
        """
        cond = self._condition_bytes(market)
        fns = self._ctf_contract.functions
        try:
            denominator = await asyncio.to_thread(fns.payoutDenominator(cond).call)
            if denominator == 0:
                return None
            total = 0
            for index, held in enumerate(holdings):
                if held:
                    numerator = await asyncio.to_thread(fns.payoutNumerators(cond, index).call)
                    total += held * numerator // denominator
            return total / 1e6
        except Exception as e:
            log.warning("Payout read failed for window %d: %s", market.window_ts, e)
            return None

    async def _redeem_position(self, market: MarketInfo) -> bool:
        condition_bytes = self._condition_bytes(market)

        # Retry up to 3 times with increasing gas
        for attempt in range(1, 4):
            try:
                # Construction, signature, envoi et attente du reçu (jusqu'à 90 s)
                # tournent dans un thread : le Sniper continue pendant ce temps.
                async with WALLET_TX_LOCK:
                    return await asyncio.to_thread(self._send_redeem_tx, condition_bytes, attempt)
            except Exception as e:
                log.warning("Redeem attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    await asyncio.sleep(5)

        log.error("Redeem failed after 3 attempts for window %d", market.window_ts)
        return False

    def _send_redeem_tx(self, condition_bytes: bytes, attempt: int) -> bool:
        """Synchrone (appelé via asyncio.to_thread). True si la transaction est confirmée."""
        wallet = Web3.to_checksum_address(self._config.wallet_address)
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
        log.error("Redeem tx reverted: %s", tx_hash.hex())
        return False
