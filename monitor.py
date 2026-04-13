"""
Monitor de performance du bot Polymarket.
Lit les logs et analyse les statistiques de trading en temps réel.
Génère un rapport toutes les heures et écrit un flag healthcheck.
"""

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOG_FILE = os.environ.get("BOT_LOG_FILE", "bot.log")
STATS_FILE = "stats.json"
HEALTHCHECK_FLAG = "healthcheck.flag"
REPORT_INTERVAL = 3600  # 1 heure


def parse_logs(log_path: str) -> dict:
    """Parse les logs du bot et extrait les métriques."""
    stats = {
        "trades": [],
        "skips": defaultdict(int),
        "errors": 0,
        "redeems": 0,
        "total_wagered": 0.0,
        "windows_observed": 0,
        "last_btc_price": None,
        "last_update": None,
    }

    if not Path(log_path).exists():
        return stats

    buy_pattern = re.compile(
        r"BUY \[(HIGH|MEDIUM)\]: (Up|Down) \| \$([\d.]+) \([\d.]+% of \$([\d.]+)\) @ \$([\d.]+)"
    )
    skip_pattern = re.compile(r"SKIP: (.+)")
    redeem_pattern = re.compile(r"Redeem CONFIRMED")
    btc_pattern = re.compile(r"BTC: \$([\d,]+\.[\d]+)")
    window_pattern = re.compile(r"Observing \d+")
    error_pattern = re.compile(r"ERROR|Exception|Traceback", re.IGNORECASE)

    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            # Prix BTC
            m = btc_pattern.search(line)
            if m:
                stats["last_btc_price"] = m.group(1).replace(",", "")
                stats["last_update"] = datetime.now().isoformat()

            # Achats
            m = buy_pattern.search(line)
            if m:
                tier, direction, amount, balance, price = m.groups()
                stats["trades"].append({
                    "tier": tier,
                    "direction": direction,
                    "amount": float(amount),
                    "balance_at_trade": float(balance),
                    "price": float(price),
                    "line": line.strip()
                })
                stats["total_wagered"] += float(amount)

            # Skips
            m = skip_pattern.search(line)
            if m:
                reason = m.group(1).split(":")[0].strip()
                stats["skips"][reason] += 1

            # Redeems
            if redeem_pattern.search(line):
                stats["redeems"] += 1

            # Fenêtres observées
            if window_pattern.search(line):
                stats["windows_observed"] += 1

            # Erreurs
            if error_pattern.search(line):
                stats["errors"] += 1

    return stats


def generate_report(stats: dict) -> str:
    """Génère un rapport lisible à partir des stats."""
    trades = stats["trades"]
    n = len(trades)
    total_wagered = stats["total_wagered"]
    redeems = stats["redeems"]
    errors = stats["errors"]
    windows = stats["windows_observed"]

    high = sum(1 for t in trades if t["tier"] == "HIGH")
    medium = sum(1 for t in trades if t["tier"] == "MEDIUM")
    up_bets = sum(1 for t in trades if t["direction"] == "Up")
    down_bets = sum(1 for t in trades if t["direction"] == "Down")

    lines = [
        "=" * 55,
        f"  RAPPORT DE PERFORMANCE — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 55,
        f"  Fenêtres observées   : {windows}",
        f"  Trades placés        : {n}  (HIGH: {high} | MEDIUM: {medium})",
        f"  Direction Up/Down    : {up_bets}/{down_bets}",
        f"  Capital engagé       : ${total_wagered:.2f}",
        f"  Positions remboursées: {redeems}",
        f"  Erreurs bot          : {errors}",
        f"  Prix BTC actuel      : ${stats.get('last_btc_price', 'N/A')}",
        "-" * 55,
    ]

    if stats["skips"]:
        lines.append("  Raisons de SKIP :")
        for reason, count in sorted(stats["skips"].items(), key=lambda x: -x[1]):
            lines.append(f"    • {reason}: {count}x")
        lines.append("-" * 55)

    if n > 0:
        avg_price = sum(t["price"] for t in trades) / n
        avg_amount = total_wagered / n
        lines.append(f"  Prix moyen d'achat   : ${avg_price:.3f}")
        lines.append(f"  Mise moyenne         : ${avg_amount:.2f}")

    lines.append("=" * 55)
    return "\n".join(lines)


async def run_monitor():
    """Boucle principale du monitor."""
    print(f"[MONITOR] Démarré — surveillance de {LOG_FILE}")

    while True:
        # Écrire le flag healthcheck (preuve que le process est vivant)
        with open(HEALTHCHECK_FLAG, "w") as f:
            f.write(datetime.now().isoformat())

        # Analyser les logs
        stats = parse_logs(LOG_FILE)

        # Sauvegarder les stats en JSON
        stats_serializable = dict(stats)
        stats_serializable["skips"] = dict(stats["skips"])
        stats_serializable["trades_count"] = len(stats["trades"])
        stats_serializable["trades"] = stats["trades"][-20:]  # garder les 20 derniers
        with open(STATS_FILE, "w") as f:
            json.dump(stats_serializable, f, indent=2)

        # Afficher le rapport
        report = generate_report(stats)
        print(report)

        await asyncio.sleep(REPORT_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_monitor())
