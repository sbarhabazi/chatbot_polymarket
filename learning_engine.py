"""
Learning Engine — Moteur d'auto-amélioration du bot Polymarket

Fonctionnement :
  1. Après chaque trade résolu (win/loss), enregistre le résultat avec ses métadonnées
  2. Recalibre la table de confiance 2D à partir des résultats réels
  3. Ajuste dynamiquement les seuils et paramètres clés
  4. Sauvegarde l'état dans learned_params.json (rechargé par le sniper à chaque fenêtre)

Les paramètres appris prennent progressivement le dessus sur les valeurs par défaut
au fur et à mesure que les données s'accumulent (minimum 20 trades par bucket).
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from logger import log

LEARNED_PARAMS_FILE = os.environ.get("LEARNED_PARAMS_FILE", "learned_params.json")
TRADE_HISTORY_FILE = os.environ.get("TRADE_HISTORY_FILE", "trade_history.json")

# Minimum de trades par bucket avant de faire confiance aux données
MIN_TRADES_FOR_CALIBRATION = 15

# Table de confiance par défaut (fallback si pas assez de données)
DEFAULT_CONFIDENCE_TABLE = {
    "50+":   {120: 0.72, 90: 0.78, 60: 0.82, 40: 0.88, 25: 0.92, 10: 0.98},
    "30-50": {120: 0.65, 90: 0.72, 60: 0.78, 40: 0.82, 25: 0.85, 10: 0.96},
    "20-30": {120: 0.58, 90: 0.65, 60: 0.72, 40: 0.76, 25: 0.78, 10: 0.90},
    "15-20": {120: 0.50, 90: 0.58, 60: 0.65, 40: 0.70, 25: 0.72, 10: 0.86},
    "12-15": {120: 0.45, 90: 0.52, 60: 0.58, 40: 0.64, 25: 0.65, 10: 0.80},
}

# Paramètres par défaut
DEFAULT_PARAMS = {
    "min_btc_distance": 12.0,
    "high_confidence_threshold": 0.72,
    "medium_confidence_threshold": 0.60,
    "high_confidence_size": 0.30,
    "medium_confidence_size": 0.20,
    "max_kelly_size": 0.20,
    "confidence_table": DEFAULT_CONFIDENCE_TABLE,
    "total_trades": 0,
    "total_wins": 0,
    "global_win_rate": None,
    "last_updated": None,
    "version": 1,
}


@dataclass
class TradeRecord:
    """Enregistrement d'un trade avec son résultat."""
    window_ts: int
    side: str                    # "Up" ou "Down"
    btc_distance: float          # distance absolue au moment du trade
    seconds_remaining: float     # temps restant quand le trade a été placé
    confidence: float            # confiance calculée par le bot
    share_price: float           # prix d'achat
    amount_usdc: float           # montant engagé
    won: Optional[bool] = None   # True/False/None (None = pas encore résolu)
    payout: float = 0.0          # montant reçu si gagné
    timestamp: float = field(default_factory=time.time)
    source: str = "live"         # "live" ou "paper"


class LearningEngine:
    """Moteur d'apprentissage et d'auto-calibration du bot."""

    def __init__(self):
        self._params = self._load_params()
        self._history = self._load_history()
        log.info("LearningEngine initialized — %d trades in history, win_rate=%s",
                 len(self._history),
                 f"{self._params.get('global_win_rate', 0):.1%}"
                 if self._params.get("global_win_rate") else "N/A")

    # ─── Persistence ──────────────────────────────────────────────

    def _load_params(self) -> dict:
        if Path(LEARNED_PARAMS_FILE).exists():
            try:
                with open(LEARNED_PARAMS_FILE, "r") as f:
                    saved = json.load(f)
                # Merge avec les défauts pour les nouvelles clés
                merged = dict(DEFAULT_PARAMS)
                merged.update(saved)
                log.info("Loaded learned params (v%d, %d trades)",
                         merged.get("version", 1), merged.get("total_trades", 0))
                return merged
            except Exception as e:
                log.warning("Failed to load learned params: %s — using defaults", e)
        return dict(DEFAULT_PARAMS)

    def _save_params(self) -> None:
        self._params["last_updated"] = time.time()
        try:
            with open(LEARNED_PARAMS_FILE, "w") as f:
                json.dump(self._params, f, indent=2)
        except Exception as e:
            log.error("Failed to save learned params: %s", e)

    def _load_history(self) -> list[dict]:
        if Path(TRADE_HISTORY_FILE).exists():
            try:
                with open(TRADE_HISTORY_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                log.warning("Failed to load trade history: %s", e)
        return []

    def _save_history(self) -> None:
        try:
            with open(TRADE_HISTORY_FILE, "w") as f:
                json.dump(self._history[-500:], f, indent=2)  # garder les 500 derniers
        except Exception as e:
            log.error("Failed to save trade history: %s", e)

    # ─── API publique ──────────────────────────────────────────────

    def record_trade(self, trade: TradeRecord) -> None:
        """Enregistre un nouveau trade (sans résultat encore)."""
        self._history.append(asdict(trade))
        self._save_history()
        log.info("LearningEngine: trade recorded (window=%d, dist=$%.1f, conf=%.2f)",
                 trade.window_ts, trade.btc_distance, trade.confidence)

    def discard_pending_trade(self, window_ts: int) -> bool:
        """Retire un trade jamais exécuté (ordre GTC annulé sans remplissage, etc.).

        Seul un trade encore en attente (``won is None``) est retiré : un résultat
        déjà enregistré n'est jamais effacé. Retourne True si un trade a été retiré.
        """
        for i, record in enumerate(self._history):
            if record["window_ts"] == window_ts and record["won"] is None:
                del self._history[i]
                self._save_history()
                log.info("LearningEngine: pending trade discarded (window=%d, never filled)",
                         window_ts)
                return True
        return False

    def has_pending_trade(self, window_ts: int) -> bool:
        return any(r["window_ts"] == window_ts and r["won"] is None for r in self._history)

    def record_result(self, window_ts: int, won: bool, payout: float = 0.0) -> None:
        """
        Enregistre le résultat d'un trade après résolution.
        Déclenche la recalibration si assez de données.
        """
        updated = False
        for record in self._history:
            if record["window_ts"] == window_ts and record["won"] is None:
                record["won"] = won
                record["payout"] = payout
                updated = True
                break

        if not updated:
            log.warning("LearningEngine: no pending trade found for window %d", window_ts)
            return

        self._save_history()
        result_str = "WIN" if won else "LOSS"
        log.info("LearningEngine: result recorded — window=%d %s payout=$%.2f",
                 window_ts, result_str, payout)

        # Recalibrer après chaque nouveau résultat
        self._recalibrate()

    def get_params(self) -> dict:
        """Retourne les paramètres actuels (appris ou défauts)."""
        return self._params

    def get_confidence(self, abs_distance: float, seconds_remaining: float) -> float:
        """
        Retourne la confiance calibrée pour une distance et un temps donné.
        Utilise les données apprises si disponibles, sinon la table par défaut.
        """
        table = self._params.get("confidence_table", DEFAULT_CONFIDENCE_TABLE)
        bucket = self._get_distance_bucket(abs_distance)

        if bucket == "<12":
            return 0.0

        if bucket not in table:
            return 0.0

        row = table[bucket]
        # Interpolation linéaire entre les colonnes
        cols = sorted([int(k) for k in row.keys()], reverse=True)
        vals = [row[str(c)] for c in cols]

        t = seconds_remaining
        if t >= cols[0]:
            return vals[0]
        if t <= cols[-1]:
            return vals[-1]

        for i in range(len(cols) - 1):
            if cols[i] >= t >= cols[i + 1]:
                frac = (cols[i] - t) / (cols[i] - cols[i + 1])
                return vals[i] + frac * (vals[i + 1] - vals[i])

        return vals[0]

    # ─── Recalibration ────────────────────────────────────────────

    def _recalibrate(self) -> None:
        """
        Recalibre tous les paramètres à partir des trades résolus.
        N'applique les changements que si les données sont statistiquement suffisantes.
        """
        resolved = [r for r in self._history if r.get("won") is not None]

        if not resolved:
            return

        total = len(resolved)
        wins = sum(1 for r in resolved if r["won"])
        global_wr = wins / total

        self._params["total_trades"] = total
        self._params["total_wins"] = wins
        self._params["global_win_rate"] = global_wr

        log.info("LearningEngine: recalibrating — %d trades, global win_rate=%.1f%%",
                 total, global_wr * 100)

        # 1. Recalibrer la table de confiance
        self._recalibrate_confidence_table(resolved)

        # 2. Ajuster MIN_BTC_DISTANCE
        self._adjust_min_distance(resolved)

        # 3. Ajuster les seuils de confiance
        self._adjust_confidence_thresholds(resolved)

        # 4. Ajuster le sizing
        self._adjust_sizing(resolved, global_wr)

        self._params["version"] = self._params.get("version", 1) + 1
        self._save_params()

        log.info("LearningEngine: params updated (v%d) — min_dist=$%.1f, "
                 "high_threshold=%.2f, medium_threshold=%.2f, "
                 "high_size=%.0f%%, medium_size=%.0f%%",
                 self._params["version"],
                 self._params["min_btc_distance"],
                 self._params["high_confidence_threshold"],
                 self._params["medium_confidence_threshold"],
                 self._params["high_confidence_size"] * 100,
                 self._params["medium_confidence_size"] * 100)

    def _recalibrate_confidence_table(self, resolved: list[dict]) -> None:
        """Met à jour la table 2D à partir des win rates observés."""
        buckets_data = defaultdict(lambda: {"wins": 0, "total": 0})

        for r in resolved:
            bucket = self._get_distance_bucket(abs(r["btc_distance"]))
            if bucket == "<12":
                continue
            buckets_data[bucket]["total"] += 1
            if r["won"]:
                buckets_data[bucket]["wins"] += 1

        new_table = {}
        for bucket, default_row in DEFAULT_CONFIDENCE_TABLE.items():
            data = buckets_data.get(bucket, {"wins": 0, "total": 0})

            if data["total"] < MIN_TRADES_FOR_CALIBRATION:
                # Pas assez de données : garder la table par défaut
                new_table[bucket] = default_row
                continue

            observed_wr = data["wins"] / data["total"]

            # Ajustement progressif : blend entre la table actuelle et l'observed
            # Plus on a de données, plus on fait confiance à l'observed
            trust = min(1.0, data["total"] / 50)  # 100% confiance à 50 trades
            current_table = self._params.get("confidence_table", DEFAULT_CONFIDENCE_TABLE)
            current_row = current_table.get(bucket, default_row)

            # Appliquer le ratio observé comme multiplicateur sur toute la ligne
            default_base = list(default_row.values())[0]  # valeur à 120s
            current_base = list(current_row.values())[0]
            base = current_base * (1 - trust) + observed_wr * trust

            # Reconstruire la ligne en gardant les ratios relatifs
            scale = base / default_base if default_base > 0 else 1.0
            scale = max(0.7, min(1.15, scale))  # limiter l'amplitude

            new_row = {}
            for time_col, default_val in default_row.items():
                new_val = round(default_val * scale, 3)
                new_val = max(0.35, min(0.98, new_val))
                new_row[time_col] = new_val

            new_table[bucket] = new_row
            log.info("  Bucket %s: observed_wr=%.1f%% (%d trades), scale=%.3f",
                     bucket, observed_wr * 100, data["total"], scale)

        self._params["confidence_table"] = new_table

    def _adjust_min_distance(self, resolved: list[dict]) -> None:
        """Ajuste MIN_BTC_DISTANCE selon la rentabilité par tranche."""
        low_dist = [r for r in resolved if 10 <= abs(r["btc_distance"]) < 15]
        if len(low_dist) >= MIN_TRADES_FOR_CALIBRATION:
            wr = sum(1 for r in low_dist if r["won"]) / len(low_dist)
            current = self._params["min_btc_distance"]
            if wr < 0.50:
                # Rentabilité trop faible pour les petites distances
                new_val = min(current + 1.0, 20.0)
                if new_val != current:
                    log.info("  min_btc_distance: %.1f → %.1f (low-dist win_rate=%.1f%%)",
                             current, new_val, wr * 100)
                    self._params["min_btc_distance"] = new_val
            elif wr > 0.70:
                # On peut descendre le seuil pour capturer plus d'opportunités
                new_val = max(current - 0.5, 10.0)
                if new_val != current:
                    log.info("  min_btc_distance: %.1f → %.1f (low-dist win_rate=%.1f%%)",
                             current, new_val, wr * 100)
                    self._params["min_btc_distance"] = new_val

    def _adjust_confidence_thresholds(self, resolved: list[dict]) -> None:
        """Ajuste les seuils HIGH/MEDIUM selon la calibration réelle."""
        if len(resolved) < 30:
            return

        # Trouver le seuil où le win rate passe au-dessus de 65%
        confidence_buckets = defaultdict(lambda: {"wins": 0, "total": 0})
        for r in resolved:
            bucket = round(r["confidence"] * 10) / 10  # arrondi à 0.1
            confidence_buckets[bucket]["total"] += 1
            if r["won"]:
                confidence_buckets[bucket]["wins"] += 1

        # Chercher le seuil MEDIUM (win_rate >= 55%)
        for threshold in [0.55, 0.58, 0.60, 0.62, 0.65]:
            above = [r for r in resolved if r["confidence"] >= threshold]
            if len(above) >= 10:
                wr = sum(1 for r in above if r["won"]) / len(above)
                if wr >= 0.55:
                    new_medium = threshold
                    current = self._params["medium_confidence_threshold"]
                    if abs(new_medium - current) >= 0.02:
                        log.info("  medium_threshold: %.2f → %.2f (wr=%.1f%% above threshold)",
                                 current, new_medium, wr * 100)
                        self._params["medium_confidence_threshold"] = new_medium
                    break

        # HIGH seuil = medium + 0.12 (garder l'écart)
        new_high = round(self._params["medium_confidence_threshold"] + 0.12, 2)
        new_high = max(0.68, min(0.85, new_high))
        if abs(new_high - self._params["high_confidence_threshold"]) >= 0.02:
            log.info("  high_threshold: %.2f → %.2f",
                     self._params["high_confidence_threshold"], new_high)
            self._params["high_confidence_threshold"] = new_high

    def _adjust_sizing(self, resolved: list[dict], global_wr: float) -> None:
        """Ajuste les tailles de position selon les performances globales."""
        if len(resolved) < 20:
            return

        current_high = self._params["high_confidence_size"]
        current_medium = self._params["medium_confidence_size"]

        # HIGH tier
        high_trades = [r for r in resolved if r["confidence"] >= self._params["high_confidence_threshold"]]
        if len(high_trades) >= 10:
            high_wr = sum(1 for r in high_trades if r["won"]) / len(high_trades)
            if high_wr > 0.72 and current_high < 0.40:
                new_high = round(min(current_high + 0.05, 0.40), 2)
                log.info("  high_size: %.0f%% → %.0f%% (HIGH win_rate=%.1f%%)",
                         current_high * 100, new_high * 100, high_wr * 100)
                self._params["high_confidence_size"] = new_high
            elif high_wr < 0.55 and current_high > 0.15:
                new_high = round(max(current_high - 0.05, 0.15), 2)
                log.info("  high_size: %.0f%% → %.0f%% (HIGH win_rate=%.1f%%)",
                         current_high * 100, new_high * 100, high_wr * 100)
                self._params["high_confidence_size"] = new_high

        # MEDIUM tier
        med_trades = [r for r in resolved
                      if self._params["medium_confidence_threshold"] <= r["confidence"] < self._params["high_confidence_threshold"]]
        if len(med_trades) >= 10:
            med_wr = sum(1 for r in med_trades if r["won"]) / len(med_trades)
            if med_wr > 0.65 and current_medium < 0.30:
                new_med = round(min(current_medium + 0.05, 0.30), 2)
                log.info("  medium_size: %.0f%% → %.0f%% (MEDIUM win_rate=%.1f%%)",
                         current_medium * 100, new_med * 100, med_wr * 100)
                self._params["medium_confidence_size"] = new_med
            elif med_wr < 0.50 and current_medium > 0.10:
                new_med = round(max(current_medium - 0.05, 0.10), 2)
                log.info("  medium_size: %.0f%% → %.0f%% (MEDIUM win_rate=%.1f%%)",
                         current_medium * 100, new_med * 100, med_wr * 100)
                self._params["medium_confidence_size"] = new_med

    # ─── Utilitaires ──────────────────────────────────────────────

    @staticmethod
    def _get_distance_bucket(abs_distance: float) -> str:
        if abs_distance >= 50:
            return "50+"
        elif abs_distance >= 30:
            return "30-50"
        elif abs_distance >= 20:
            return "20-30"
        elif abs_distance >= 15:
            return "15-20"
        elif abs_distance >= 12:
            return "12-15"
        else:
            return "<12"

    def summary(self) -> str:
        p = self._params
        total = p.get("total_trades", 0)
        wr = p.get("global_win_rate")
        wr_str = f"{wr:.1%}" if wr else "N/A"
        return (
            f"LearningEngine v{p.get('version', 1)} | "
            f"{total} trades | win_rate={wr_str} | "
            f"min_dist=${p['min_btc_distance']:.1f} | "
            f"thresholds={p['medium_confidence_threshold']:.2f}/{p['high_confidence_threshold']:.2f} | "
            f"sizing={p['medium_confidence_size']:.0%}/{p['high_confidence_size']:.0%}"
        )
