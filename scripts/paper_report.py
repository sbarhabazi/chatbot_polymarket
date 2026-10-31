"""Rapport de performance à partir de trade_history.json (papier ou réel).

Usage (dans le conteneur) :
    docker exec polybot python scripts/paper_report.py            # trades papier
    docker exec polybot python scripts/paper_report.py --source live
    docker exec polybot python scripts/paper_report.py --source all

La question clé : par tranche de confiance, le taux de gain observé dépasse-t-il
le prix moyen payé ? Une part achetée 0,70 $ ne rapporte que si elle gagne plus
de ~70 % du temps (un peu plus, à cause des frais). Sinon la stratégie perd de
l'argent, quelle que soit la confiance affichée par le bot.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from learning_engine import TRADE_HISTORY_FILE  # noqa: E402

FEE_RATE = 0.072
BUCKETS = [(0.0, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.90), (0.90, 1.01)]


def breakeven(price: float) -> float:
    """Taux de gain minimal pour qu'un achat au prix `price` soit rentable."""
    # gain net si victoire : (1 - p)(1 - fee) ; perte sinon : p
    return price / (price + (1 - price) * (1 - FEE_RATE))


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for t in trades if t["won"])
    cost = sum(t["amount_usdc"] for t in trades)
    payout = sum(t.get("payout", 0.0) for t in trades)
    avg_price = sum(t["share_price"] for t in trades) / n
    return {
        "n": n,
        "wins": wins,
        "win_rate": wins / n,
        "avg_conf": sum(t["confidence"] for t in trades) / n,
        "avg_price": avg_price,
        "breakeven": breakeven(avg_price),
        "cost": cost,
        "payout": payout,
        "pnl": payout - cost,
        "roi": (payout - cost) / cost if cost else 0.0,
    }


def build_report(history: list[dict], source: str = "paper") -> str:
    trades = [t for t in history if source == "all" or t.get("source") == source]
    resolved = [t for t in trades if t.get("won") is not None]
    pending = len(trades) - len(resolved)

    lines = [f"=== Rapport {source} — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC ==="]
    if not trades:
        lines.append("Aucun trade.")
        return "\n".join(lines)

    first = min(t["timestamp"] for t in trades)
    days = max((max(t["timestamp"] for t in trades) - first) / 86400, 1 / 24)
    lines.append(f"Période : {datetime.fromtimestamp(first, timezone.utc):%Y-%m-%d} "
                 f"→ {days:.1f} jours | {len(resolved)} résolus, {pending} en attente")

    s = summarize(resolved)
    if not s["n"]:
        lines.append("Aucun trade résolu pour l'instant.")
        return "\n".join(lines)

    lines += [
        f"Gagnés     : {s['wins']}/{s['n']} ({s['win_rate']:.1%})",
        f"Prix moyen : ${s['avg_price']:.3f} → seuil de rentabilité {s['breakeven']:.1%}",
        f"Misé       : ${s['cost']:.2f} | reçu ${s['payout']:.2f} | "
        f"P&L ${s['pnl']:+.2f} ({s['roi']:+.1%})",
        "",
        "Par confiance : n | gagnés | seuil | P&L",
    ]
    for lo, hi in BUCKETS:
        b = summarize([t for t in resolved if lo <= t["confidence"] < hi])
        if not b["n"]:
            continue
        verdict = "OK " if b["win_rate"] > b["breakeven"] else "KO "
        label = f"{lo:.2f}-{min(hi, 1.0):.2f}"
        lines.append(f"  {verdict}{label} : {b['n']:4d} | {b['win_rate']:6.1%} | "
                     f"{b['breakeven']:6.1%} | ${b['pnl']:+.2f}")

    if s["n"] < 100:
        lines += ["", f"Attention : {s['n']} trades seulement — trop peu pour conclure "
                      "(viser au moins 100 à 200)."]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", choices=["paper", "live", "all"], default="paper")
    parser.add_argument("--file", default=TRADE_HISTORY_FILE)
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"Aucun historique : {path}")
        return
    print(build_report(json.loads(path.read_text()), args.source))


if __name__ == "__main__":
    main()
