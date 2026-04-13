"""
Optimiseur d'algorithme automatique pour le bot Polymarket.

Analyse les données historiques de trading (stats.json + logs)
et propose des ajustements optimaux pour les paramètres clés :
  - Table de confiance 2D
  - Seuils d'entrée (MIN_BTC_DISTANCE, confidence thresholds)
  - Sizing (HIGH/MEDIUM fractions)
  - Pénalités volatilité/momentum

Usage : python optimizer.py [--apply]
  --apply : écrit directement les nouvelles valeurs dans .env
"""

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path

STATS_FILE = "stats.json"
LOG_FILE = os.environ.get("BOT_LOG_FILE", "bot.log")
ENV_FILE = ".env"

# Table de confiance par défaut (baseline)
DEFAULT_CONFIDENCE_TABLE = {
    "50+":   {120: 0.72, 90: 0.78, 82: 0.82, 88: 0.88, 92: 0.92, 98: 0.98},
    "30-50": {120: 0.65, 90: 0.72, 78: 0.78, 82: 0.82, 85: 0.85, 96: 0.96},
    "20-30": {120: 0.58, 90: 0.65, 72: 0.72, 76: 0.76, 78: 0.78, 90: 0.90},
    "15-20": {120: 0.50, 90: 0.58, 65: 0.65, 70: 0.70, 72: 0.72, 86: 0.86},
    "12-15": {120: 0.45, 90: 0.52, 58: 0.58, 64: 0.64, 65: 0.65, 80: 0.80},
}


def parse_trade_outcomes(log_path: str) -> list:
    """
    Parse les logs pour extraire :
    - Distance BTC au moment du trade
    - Temps restant
    - Direction pariée
    - Résultat (gagné/perdu via redeem ou absence)
    """
    trades = []
    buy_pattern = re.compile(
        r"BUY \[(HIGH|MEDIUM)\]: (Up|Down) .* dist=\$([-\d.]+) .* conf=([\d.]+)"
    )
    redeem_pattern = re.compile(r"Redeemed successfully: window (\d+)")
    window_pattern = re.compile(r"window (\d+)")

    redeemed_windows = set()

    if not Path(log_path).exists():
        return trades

    with open(log_path, "r", errors="ignore") as f:
        lines = f.readlines()

    # Premier pass : collecter les windows remboursées
    for line in lines:
        m = redeem_pattern.search(line)
        if m:
            redeemed_windows.add(m.group(1))

    # Deuxième pass : analyser les trades
    for line in lines:
        m = buy_pattern.search(line)
        if m:
            tier, direction, dist, conf = m.groups()
            wm = window_pattern.search(line)
            window_id = wm.group(1) if wm else None
            trades.append({
                "tier": tier,
                "direction": direction,
                "distance": abs(float(dist)),
                "confidence": float(conf),
                "window": window_id,
                "won": window_id in redeemed_windows if window_id else None,
            })

    return trades


def analyze_performance(trades: list) -> dict:
    """Calcule les win rates par bucket distance."""
    buckets = defaultdict(lambda: {"wins": 0, "total": 0})

    for t in trades:
        d = t["distance"]
        if t["won"] is None:
            continue  # résultat inconnu
        if d >= 50:
            key = "50+"
        elif d >= 30:
            key = "30-50"
        elif d >= 20:
            key = "20-30"
        elif d >= 15:
            key = "15-20"
        elif d >= 12:
            key = "12-15"
        else:
            key = "<12"

        buckets[key]["total"] += 1
        if t["won"]:
            buckets[key]["wins"] += 1

    results = {}
    for key, data in buckets.items():
        total = data["total"]
        if total > 0:
            results[key] = {
                "win_rate": data["wins"] / total,
                "total": total,
                "wins": data["wins"],
            }
    return results


def suggest_optimizations(trades: list, performance: dict) -> dict:
    """
    Génère des recommandations d'ajustement basées sur les données réelles.
    Principe : si win_rate observé < confiance calibrée → réduire la confiance
               si win_rate observé > confiance calibrée → opportunité d'augmenter la taille
    """
    suggestions = {}

    if not trades:
        suggestions["status"] = "Pas assez de données (0 trades avec résultats connus)"
        suggestions["min_trades_needed"] = 50
        return suggestions

    known = [t for t in trades if t["won"] is not None]
    if len(known) < 20:
        suggestions["status"] = f"Données insuffisantes ({len(known)} trades avec résultats). Minimum recommandé: 50"
        return suggestions

    suggestions["status"] = "OK"
    suggestions["total_analyzed"] = len(known)
    suggestions["win_rate_global"] = sum(1 for t in known if t["won"]) / len(known)

    # Ajustements par bucket
    adjustments = {}
    for bucket, data in performance.items():
        if data["total"] >= 5:  # minimum statistique
            observed_wr = data["win_rate"]
            # Comparer avec la table calibrée (valeur à 120s comme référence)
            if bucket in DEFAULT_CONFIDENCE_TABLE:
                calibrated = list(DEFAULT_CONFIDENCE_TABLE[bucket].values())[0]
                delta = observed_wr - calibrated
                if abs(delta) > 0.05:  # seulement si écart significatif
                    adjustments[bucket] = {
                        "observed_win_rate": round(observed_wr, 3),
                        "calibrated_confidence": calibrated,
                        "delta": round(delta, 3),
                        "recommendation": "↑ Augmenter taille" if delta > 0 else "↓ Réduire taille ou seuil",
                        "sample_size": data["total"],
                    }

    suggestions["bucket_adjustments"] = adjustments

    # Recommandation MIN_BTC_DISTANCE
    low_dist_trades = [t for t in known if t["distance"] < 15]
    if len(low_dist_trades) >= 10:
        low_wr = sum(1 for t in low_dist_trades if t["won"]) / len(low_dist_trades)
        if low_wr < 0.52:
            suggestions["min_btc_distance"] = {
                "current": 12.0,
                "recommended": 15.0,
                "reason": f"Win rate trop bas ({low_wr:.1%}) pour distances < $15",
            }

    # Recommandation sizing
    high_trades = [t for t in known if t["tier"] == "HIGH"]
    if len(high_trades) >= 10:
        high_wr = sum(1 for t in high_trades if t["won"]) / len(high_trades)
        if high_wr > 0.75:
            suggestions["high_confidence_size"] = {
                "current": 0.30,
                "recommended": min(0.40, 0.30 + 0.05),
                "reason": f"Excellent win rate ({high_wr:.1%}) — augmenter la taille HIGH",
            }
        elif high_wr < 0.60:
            suggestions["high_confidence_size"] = {
                "current": 0.30,
                "recommended": max(0.15, 0.30 - 0.05),
                "reason": f"Win rate décevant ({high_wr:.1%}) — réduire la taille HIGH",
            }

    return suggestions


def apply_suggestions(suggestions: dict, env_path: str) -> None:
    """Applique les suggestions d'optimisation dans le fichier .env."""
    if suggestions.get("status") != "OK":
        print(f"[OPTIMIZER] Impossible d'appliquer : {suggestions.get('status')}")
        return

    if not Path(env_path).exists():
        print(f"[OPTIMIZER] Fichier {env_path} introuvable.")
        return

    with open(env_path, "r") as f:
        content = f.read()

    changes = []

    # MIN_BTC_DISTANCE
    if "min_btc_distance" in suggestions:
        new_val = suggestions["min_btc_distance"]["recommended"]
        content = re.sub(
            r"MIN_BTC_DISTANCE=[\d.]+",
            f"MIN_BTC_DISTANCE={new_val}",
            content
        )
        changes.append(f"MIN_BTC_DISTANCE → {new_val}")

    # HIGH_CONFIDENCE_SIZE
    if "high_confidence_size" in suggestions:
        new_val = suggestions["high_confidence_size"]["recommended"]
        content = re.sub(
            r"HIGH_CONFIDENCE_SIZE=[\d.]+",
            f"HIGH_CONFIDENCE_SIZE={new_val}",
            content
        )
        changes.append(f"HIGH_CONFIDENCE_SIZE → {new_val}")

    with open(env_path, "w") as f:
        f.write(content)

    if changes:
        print("[OPTIMIZER] Paramètres mis à jour :")
        for c in changes:
            print(f"  ✓ {c}")
    else:
        print("[OPTIMIZER] Aucun changement nécessaire.")


def main():
    parser = argparse.ArgumentParser(description="Optimiseur de l'algorithme Polymarket")
    parser.add_argument("--apply", action="store_true", help="Appliquer les suggestions dans .env")
    args = parser.parse_args()

    print("[OPTIMIZER] Analyse des performances...")

    trades = parse_trade_outcomes(LOG_FILE)
    performance = analyze_performance(trades)
    suggestions = suggest_optimizations(trades, performance)

    print(json.dumps(suggestions, indent=2, ensure_ascii=False))

    if args.apply:
        apply_suggestions(suggestions, ENV_FILE)
    else:
        print("\n[INFO] Utiliser --apply pour appliquer automatiquement les ajustements.")


if __name__ == "__main__":
    main()
