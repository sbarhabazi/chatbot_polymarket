"""
Balance Guard — Process D

Surveille le solde USDC.e en continu et gère trois états :

  NORMAL   (solde >= 15$) : trading standard, demi-Kelly plafonné 20 %
  SURVIE   (solde < 15$)  : trading prudent, quart de Kelly plafonné 10 %,
                            confiance minimale relevée
  DERNIER  (solde < 3$)   : plus aucun nouvel ordre (capital préservé)
  RUINE    (solde < 0.50$): arrêt du bot + alerte pour recharger

Plus le capital baisse, plus le risque par trade baisse : un solde réduit
n'est jamais une raison de miser une plus grande part de ce qui reste.

Le guard écrit l'état courant dans un fichier partagé lu par le sniper.
À solde nul, il déclenche l'arrêt propre du process principal via l'event
shutdown_event, puis écrit un fichier sentinel pour que le container
Docker se relance en mode "attente recharge".
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from web3 import Web3

from contracts.addresses import USDC_E
from logger import log

STATE_FILE   = "balance_guard_state.json"
SENTINEL_FILE = "RUINE_ATTENTE_RECHARGE"

BALANCE_ABI = [{
    "name": "balanceOf", "type": "function",
    "inputs": [{"name": "account", "type": "address"}],
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
}]

# Seuils
THRESHOLD_NORMAL   = 15.0   # >= 15$ → mode normal
THRESHOLD_SURVIE   = 3.0    # >= 3$ et < 15$ → mode survie (quart de Kelly, cap 10 %)
THRESHOLD_DERNIER  = 0.50   # >= 0.50$ et < 3$ → plus de nouvel ordre
# < 0.50$ → RUINE → arrêt + alerte

# Maximum Kelly size par mode
KELLY_CAP = {
    "NORMAL":  0.20,   # 20% max
    "SURVIE":  0.10,   # 10% max
    "DERNIER": 0.00,   # aucun nouvel ordre
    "RUINE":   0.00,   # rien
}

# Fraction Kelly par mode
KELLY_FRACTION = {
    "NORMAL":  0.50,   # Half-Kelly
    "SURVIE":  0.25,   # Quarter-Kelly
    "DERNIER": 0.00,
    "RUINE":   0.00,
}

# Confiance minimale par mode
MIN_CONFIDENCE = {
    "NORMAL":  0.60,
    "SURVIE":  0.70,
    "DERNIER": 1.01,   # Jamais (confiance plafonnée à 0.98)
    "RUINE":   1.01,   # Jamais
}


class BalanceGuard:
    """Surveille le solde et gère le cycle de vie du bot."""

    def __init__(
        self,
        wallet_address: str,
        w3: Web3,
        shutdown_event: asyncio.Event,
        check_interval: int = 60,
    ):
        self._wallet = Web3.to_checksum_address(wallet_address)
        self._w3 = w3
        self._shutdown_event = shutdown_event
        self._check_interval = check_interval
        self._contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_E),
            abi=BALANCE_ABI,
        )
        self._current_mode = "NORMAL"
        self._last_balance: float | None = None
        self._ruine_notified = False

        # Charger l'état précédent si existant
        self._load_state()

    def _load_state(self) -> None:
        if Path(STATE_FILE).exists():
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                self._current_mode = state.get("mode", "NORMAL")
                self._last_balance = state.get("balance")
                log.info("BalanceGuard: état restauré — mode=%s, solde=$%.2f",
                         self._current_mode, self._last_balance or 0)
            except Exception:
                pass

    def _save_state(self, balance: float, mode: str) -> None:
        state = {
            "timestamp": time.time(),
            "balance": balance,
            "mode": mode,
            "kelly_cap": KELLY_CAP[mode],
            "kelly_fraction": KELLY_FRACTION[mode],
            "min_confidence": MIN_CONFIDENCE[mode],
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.error("BalanceGuard: save state failed: %s", e)

    @staticmethod
    def get_mode_for_balance(balance: float) -> str:
        """Retourne le mode correspondant au solde donné."""
        if balance >= THRESHOLD_NORMAL:
            return "NORMAL"
        elif balance >= THRESHOLD_SURVIE:
            return "SURVIE"
        elif balance >= THRESHOLD_DERNIER:
            return "DERNIER"
        else:
            return "RUINE"

    @staticmethod
    def params_for_mode(mode: str) -> dict:
        """Paramètres de risque d'un mode, toujours tirés des tables ci-dessus."""
        if mode not in KELLY_CAP:
            mode = "NORMAL"
        return {
            "mode": mode,
            "kelly_cap": KELLY_CAP[mode],
            "kelly_fraction": KELLY_FRACTION[mode],
            "min_confidence": MIN_CONFIDENCE[mode],
        }

    @staticmethod
    def read_current_mode() -> dict:
        """Appelé par le Sniper pour connaître le mode actuel.

        Seul le nom du mode est lu dans le fichier d'état : les plafonds viennent
        du code, pour qu'un fichier écrit par une ancienne version (ex. cap 100 %)
        ne puisse pas réactiver des paramètres plus risqués.
        """
        mode = "NORMAL"
        try:
            if Path(STATE_FILE).exists():
                with open(STATE_FILE) as f:
                    mode = json.load(f).get("mode", "NORMAL")
        except Exception:
            pass
        return BalanceGuard.params_for_mode(mode)

    async def get_balance(self) -> float:
        try:
            raw = await asyncio.to_thread(
                self._contract.functions.balanceOf(self._wallet).call
            )
            return raw / 1e6
        except Exception as e:
            log.warning("BalanceGuard: lecture solde échouée: %s", e)
            return self._last_balance or 0.0

    async def run(self) -> None:
        log.info("BalanceGuard (Process D) started — vérification toutes les %ds", self._check_interval)
        while True:
            try:
                await self._check()
            except Exception as e:
                log.error("BalanceGuard error: %s", e, exc_info=True)
            await asyncio.sleep(self._check_interval)

    async def _check(self) -> None:
        balance = await self.get_balance()
        new_mode = self.get_mode_for_balance(balance)

        # Log changement de mode
        if new_mode != self._current_mode:
            log.warning(
                "BalanceGuard: MODE %s → %s (solde=$%.2f)",
                self._current_mode, new_mode, balance,
            )
            self._current_mode = new_mode

        self._save_state(balance, new_mode)
        self._last_balance = balance

        # ─── RUINE ────────────────────────────────────────────────
        if new_mode == "RUINE":
            if not self._ruine_notified:
                self._ruine_notified = True
                log.critical(
                    "BalanceGuard: RUINE DÉTECTÉE — solde=$%.4f < $%.2f | "
                    "ARRÊT DU BOT + écriture sentinel",
                    balance, THRESHOLD_DERNIER,
                )
                # Fichier sentinel lu par le cron pour envoyer l'alerte
                sentinel = {
                    "timestamp": time.time(),
                    "balance": balance,
                    "reason": "Solde insuffisant pour trader — recharge requise",
                    "message_fr": (
                        f"Le bot s'est arrêté automatiquement. "
                        f"Solde final: ${balance:.4f} USDC.e. "
                        f"Recharge le wallet pour relancer."
                    ),
                }
                try:
                    with open(SENTINEL_FILE, "w") as f:
                        json.dump(sentinel, f, indent=2)
                    log.critical("BalanceGuard: sentinel écrit → %s", SENTINEL_FILE)
                except Exception as e:
                    log.error("BalanceGuard: impossible d'écrire le sentinel: %s", e)

                # Déclencher l'arrêt propre
                self._shutdown_event.set()
            return

        # ─── DERNIER RECOURS ──────────────────────────────────────
        elif new_mode == "DERNIER":
            log.warning(
                "BalanceGuard: MODE DERNIER RECOURS — solde=$%.2f | "
                "aucun nouvel ordre, recharge recommandée",
                balance,
            )

        # ─── SURVIE ───────────────────────────────────────────────
        elif new_mode == "SURVIE":
            log.info(
                "BalanceGuard: MODE SURVIE — solde=$%.2f | "
                "quart de Kelly, cap 10%%",
                balance,
            )

        # ─── NORMAL ───────────────────────────────────────────────
        else:
            log.debug("BalanceGuard: solde=$%.2f, mode=NORMAL", balance)
