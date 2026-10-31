"""
Live Optimizer — Process E

Boucle d'optimisation interne qui tourne toutes les 30 minutes
directement dans le processus du bot (pas de cron externe).

Fonctionnement :
  - Toutes les 30 minutes, relit trade_history.json
  - Si le nombre de trades résolus a augmenté depuis la dernière
    optimisation, déclenche une recalibration via LearningEngine
  - Recharge learned_params.json → Sniper les utilisera automatiquement
    au prochain cycle (PriceAnalyzer.compute_distance_signal lit les params)
  - Détecte et signale les patterns de perte persistants

Seuils d'activation (moins restrictifs que le cron externe) :
  - Min 5 trades résolus (au lieu de 50) pour la première calibration
  - Min 2 nouveaux trades depuis la dernière optimisation
  - Pas de limite haute — recalibre dès que de nouvelles données arrivent
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from learning_engine import LEARNED_PARAMS_FILE as _PARAMS_PATH
from learning_engine import TRADE_HISTORY_FILE as _HISTORY_PATH
from learning_engine import LearningEngine
from logger import log

# Mêmes fichiers que le LearningEngine (DATA_DIR ou variables d'environnement)
TRADE_HISTORY_FILE = Path(_HISTORY_PATH)
LEARNED_PARAMS_FILE = Path(_PARAMS_PATH)
OPTIMIZE_INTERVAL = 1800  # 30 minutes en secondes
MIN_TRADES_FOR_FIRST_CALIBRATION = 5
MIN_NEW_TRADES_TO_TRIGGER = 2


class LiveOptimizer:
    """Process E: Optimisation continue toutes les 30 minutes."""

    def __init__(self, learning_engine: LearningEngine):
        self._le = learning_engine
        self._last_resolved_count: int = self._count_resolved_trades()
        self._last_optimize_time: float = time.time()
        self._optimizations_done: int = 0

        log.info(
            "LiveOptimizer (Process E) démarré — optimisation toutes les %ds | "
            "%d trades résolus au démarrage",
            OPTIMIZE_INTERVAL,
            self._last_resolved_count,
        )

    async def run(self) -> None:
        """Boucle principale — tourne indéfiniment toutes les 30 minutes."""
        while True:
            try:
                # Attendre 30 minutes entre chaque cycle
                await asyncio.sleep(OPTIMIZE_INTERVAL)
                await self._run_optimization_cycle()
            except asyncio.CancelledError:
                log.info("LiveOptimizer: arrêt propre")
                return
            except Exception as e:
                log.error("LiveOptimizer: erreur inattendue: %s", e, exc_info=True)
                await asyncio.sleep(60)  # Pause courte avant de réessayer

    async def _run_optimization_cycle(self) -> None:
        """Exécute un cycle d'optimisation."""
        now = time.time()
        elapsed = now - self._last_optimize_time

        resolved_count = self._count_resolved_trades()
        new_trades = resolved_count - self._last_resolved_count

        log.info(
            "LiveOptimizer: cycle d'optimisation | %d trades résolus (+%d nouveaux) | "
            "dernier cycle il y a %.0fmin",
            resolved_count,
            new_trades,
            elapsed / 60,
        )

        # Conditions d'activation
        if resolved_count < MIN_TRADES_FOR_FIRST_CALIBRATION:
            log.info(
                "LiveOptimizer: skip — seulement %d/%d trades résolus",
                resolved_count,
                MIN_TRADES_FOR_FIRST_CALIBRATION,
            )
            return

        if new_trades < MIN_NEW_TRADES_TO_TRIGGER:
            log.info(
                "LiveOptimizer: skip — seulement %d nouveaux trades (seuil: %d)",
                new_trades,
                MIN_NEW_TRADES_TO_TRIGGER,
            )
            return

        # Déclencher la recalibration
        log.info(
            "LiveOptimizer: RECALIBRATION (%d trades résolus, +%d nouveaux) — "
            "optimisation #%d",
            resolved_count,
            new_trades,
            self._optimizations_done + 1,
        )

        try:
            # Recharger l'historique dans le learning engine
            self._le._history = self._le._load_history()

            # Déclencher la recalibration complète
            self._le._recalibrate()

            self._optimizations_done += 1
            self._last_resolved_count = resolved_count
            self._last_optimize_time = now

            # Logger les nouveaux paramètres
            params = self._le.get_params()
            log.info(
                "LiveOptimizer: paramètres mis à jour (v%d) | "
                "min_dist=$%.1f | seuils=%.2f/%.2f | sizing=%.0f%%/%.0f%% | "
                "win_rate=%s",
                params.get("version", 1),
                params.get("min_btc_distance", 12.0),
                params.get("medium_confidence_threshold", 0.60),
                params.get("high_confidence_threshold", 0.72),
                params.get("medium_confidence_size", 0.20) * 100,
                params.get("high_confidence_size", 0.30) * 100,
                f"{params['global_win_rate']:.1%}" if params.get("global_win_rate") else "N/A",
            )

            # Analyser les patterns de perte
            self._analyze_loss_patterns()

        except Exception as e:
            log.error("LiveOptimizer: recalibration échouée: %s", e, exc_info=True)

    def _count_resolved_trades(self) -> int:
        """Compte les trades résolus dans l'historique."""
        if not TRADE_HISTORY_FILE.exists():
            return 0
        try:
            with open(TRADE_HISTORY_FILE) as f:
                trades = json.load(f)
            return sum(
                1 for t in trades
                if t.get("won") is not None or t.get("outcome") in ("win", "loss")
            )
        except Exception:
            return 0

    def _analyze_loss_patterns(self) -> None:
        """Analyse les patterns de perte pour détecter des anomalies."""
        if not TRADE_HISTORY_FILE.exists():
            return
        try:
            with open(TRADE_HISTORY_FILE) as f:
                trades = json.load(f)

            resolved = [t for t in trades if t.get("won") is not None]
            if len(resolved) < 5:
                return

            # Pattern 1: Série de pertes consécutives récentes
            recent = resolved[-10:]  # 10 derniers trades
            recent_losses = sum(1 for t in recent if not t.get("won", True))
            if recent_losses >= 7:
                log.warning(
                    "LiveOptimizer: ALERTE — %d pertes sur les %d derniers trades "
                    "(taux=%.0f%%) — le marché est peut-être en mode range",
                    recent_losses, len(recent),
                    recent_losses / len(recent) * 100,
                )

            # Pattern 2: Trades avec faible distance perdent souvent
            low_dist_trades = [
                t for t in resolved
                if abs(t.get("btc_distance", 99)) < 15
            ]
            if len(low_dist_trades) >= 5:
                wr = sum(1 for t in low_dist_trades if t.get("won", False)) / len(low_dist_trades)
                if wr < 0.45:
                    log.warning(
                        "LiveOptimizer: trades distance<15$ win_rate=%.0f%% sur %d trades "
                        "— augmentation de MIN_BTC_DISTANCE recommandée",
                        wr * 100, len(low_dist_trades),
                    )

            # Pattern 3: Trades placés tôt dans la fenêtre (<60s) vs tard (>60s)
            early_trades = [t for t in resolved if t.get("seconds_remaining", 0) > 60]
            late_trades = [t for t in resolved if t.get("seconds_remaining", 0) <= 60]

            if len(early_trades) >= 5 and len(late_trades) >= 5:
                early_wr = sum(1 for t in early_trades if t.get("won", False)) / len(early_trades)
                late_wr = sum(1 for t in late_trades if t.get("won", False)) / len(late_trades)
                log.info(
                    "LiveOptimizer: win_rate EARLY(>60s)=%.0f%% (%d trades) vs "
                    "LATE(≤60s)=%.0f%% (%d trades)",
                    early_wr * 100, len(early_trades),
                    late_wr * 100, len(late_trades),
                )
                if early_wr < 0.45 and late_wr > 0.60:
                    log.info(
                        "LiveOptimizer: INSIGHT — les trades tardifs performent mieux. "
                        "Réduire ENTRY_WINDOW_SECONDS pourrait améliorer le win rate."
                    )

        except Exception as e:
            log.warning("LiveOptimizer: analyse patterns échouée: %s", e)

    def status(self) -> dict:
        """Retourne le statut courant du LiveOptimizer."""
        return {
            "optimizations_done": self._optimizations_done,
            "last_resolved_count": self._last_resolved_count,
            "next_optimization_in": max(
                0,
                int(OPTIMIZE_INTERVAL - (time.time() - self._last_optimize_time))
            ),
            "interval_seconds": OPTIMIZE_INTERVAL,
        }
