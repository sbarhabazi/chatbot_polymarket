"""
Profit Taker — Virement automatique vers le wallet personnel

Logique :
  - Vérifie le solde USDC.e toutes les 5 minutes
  - Si le solde dépasse 100 $, transfère 50 $ vers le wallet personnel
  - Garde toujours un minimum de trading_reserve dans le wallet de trading
  - Log chaque virement et met à jour un fichier de suivi (profit_history.json)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config import BotConfig
from contracts.addresses import USDC_E
from logger import log
from tx_lock import WALLET_TX_LOCK

PROFIT_HISTORY_FILE = "profit_history.json"

# ABI minimal pour transfert ERC-20
TRANSFER_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]


class ProfitTaker:
    """
    Surveille le solde et transfère automatiquement les profits
    vers le wallet personnel quand le seuil est atteint.
    """

    def __init__(
        self,
        config: BotConfig,
        w3: Web3,
        personal_wallet: str,
        trigger_balance: float = 100.0,   # Seuil de déclenchement ($)
        transfer_amount: float = 50.0,    # Montant à transférer ($)
        trading_reserve: float = 40.0,    # Solde minimum à garder pour trader
        check_interval: int = 300,        # Vérification toutes les 5 min
    ):
        self._config = config
        self._w3 = w3
        self._personal_wallet = Web3.to_checksum_address(personal_wallet)
        self._trigger_balance = trigger_balance
        self._transfer_amount = transfer_amount
        self._trading_reserve = trading_reserve
        self._check_interval = check_interval
        self._history = self._load_history()

        self._usdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_E),
            abi=TRANSFER_ABI,
        )

        log.info(
            "ProfitTaker initialized — trigger=$%.0f, transfer=$%.0f, "
            "reserve=$%.0f, destination=%s...%s",
            trigger_balance, transfer_amount, trading_reserve,
            personal_wallet[:8], personal_wallet[-4:],
        )

    # ─── Persistence ──────────────────────────────────────────────

    def _load_history(self) -> list[dict]:
        if Path(PROFIT_HISTORY_FILE).exists():
            try:
                with open(PROFIT_HISTORY_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_history(self, entry: dict) -> None:
        self._history.append(entry)
        try:
            with open(PROFIT_HISTORY_FILE, "w") as f:
                json.dump(self._history, f, indent=2)
        except Exception as e:
            log.error("ProfitTaker: failed to save history: %s", e)

    # ─── Boucle principale ─────────────────────────────────────────

    async def run(self) -> None:
        log.info("ProfitTaker (Process C) started — checking every %ds", self._check_interval)
        while True:
            try:
                await self._check_and_transfer()
            except Exception as e:
                log.error("ProfitTaker error: %s", e, exc_info=True)
            await asyncio.sleep(self._check_interval)

    async def _check_and_transfer(self) -> None:
        wallet = Web3.to_checksum_address(self._config.wallet_address)

        # Lire le solde actuel
        try:
            balance_raw = await asyncio.to_thread(
                self._usdc_contract.functions.balanceOf(wallet).call
            )
            balance = balance_raw / 1e6
        except Exception as e:
            log.warning("ProfitTaker: failed to read balance: %s", e)
            return

        log.info("ProfitTaker: balance=$%.2f (trigger=$%.0f)", balance, self._trigger_balance)

        # Vérifier si le seuil est atteint (solde >= trigger)
        if balance < self._trigger_balance:
            return

        # Montant à transférer = solde - trading_reserve ($50)
        # Le solde résiduel est toujours exactement trading_reserve après le transfert
        actual_transfer = balance - self._trading_reserve

        if actual_transfer < 1.0:
            log.warning(
                "ProfitTaker: SKIP — montant calculé trop faible ($%.2f)",
                actual_transfer,
            )
            return

        log.info(
            "ProfitTaker: TRIGGER! Balance $%.2f >= $%.0f — transferring $%.2f "
            "(solde - $%.0f reserve) to %s...%s",
            balance, self._trigger_balance,
            actual_transfer, self._trading_reserve,
            str(self._personal_wallet)[:8], str(self._personal_wallet)[-4:],
        )

        success, tx_hash = await self._transfer_usdc(wallet, actual_transfer)

        entry = {
            "timestamp": time.time(),
            "balance_before": balance,
            "amount_transferred": actual_transfer,
            "balance_after_target": self._trading_reserve,
            "destination": str(self._personal_wallet),
            "success": success,
            "tx_hash": tx_hash,
        }
        self._save_history(entry)

        if success:
            log.info(
                "ProfitTaker: TRANSFER OK — $%.2f → %s...%s | tx: %s | solde résiduel cible: $%.0f",
                actual_transfer,
                str(self._personal_wallet)[:8], str(self._personal_wallet)[-4:],
                tx_hash, self._trading_reserve,
            )
            total_transferred = sum(h["amount_transferred"] for h in self._history if h["success"])
            log.info("ProfitTaker: total profits secured = $%.2f", total_transferred)
        else:
            log.error("ProfitTaker: TRANSFER FAILED — tx: %s", tx_hash)

    async def _transfer_usdc(self, from_wallet: str, amount_usd: float) -> tuple[bool, str]:
        """Transfère amount_usd USDC.e vers le wallet personnel."""
        amount_raw = int(amount_usd * 1e6)  # USDC.e a 6 décimales

        for attempt in range(1, 4):
            try:
                # Envoi et attente du reçu (jusqu'à 90 s) dans un thread :
                # la boucle asyncio (Sniper, flux de prix) n'est pas bloquée.
                async with WALLET_TX_LOCK:
                    return await asyncio.to_thread(
                        self._send_transfer_tx, from_wallet, amount_raw, attempt
                    )
            except Exception as e:
                log.warning("ProfitTaker: attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    await asyncio.sleep(5)

        return False, "failed_after_3_attempts"

    def _send_transfer_tx(self, from_wallet: str, amount_raw: int, attempt: int) -> tuple[bool, str]:
        """Synchrone (appelé via asyncio.to_thread)."""
        nonce = self._w3.eth.get_transaction_count(from_wallet, "pending")
        gas_price = int(self._w3.eth.gas_price * (1.5 + attempt * 0.5))

        tx = self._usdc_contract.functions.transfer(
            self._personal_wallet,
            amount_raw,
        ).build_transaction({
            "chainId": 137,
            "from": from_wallet,
            "nonce": nonce,
            "gasPrice": gas_price,
            "gas": 100_000,
        })

        signed = self._w3.eth.account.sign_transaction(tx, self._config.private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info("ProfitTaker: tx sent (attempt %d): %s", attempt, tx_hash.hex())

        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        if receipt["status"] == 1:
            return True, tx_hash.hex()
        log.error("ProfitTaker: tx reverted: %s", tx_hash.hex())
        return False, tx_hash.hex()
