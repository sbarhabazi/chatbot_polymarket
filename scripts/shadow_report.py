"""Rapport de calibration à partir du journal fantôme (toutes les fenêtres).

Usage (dans le conteneur) :
    docker exec polybot python scripts/shadow_report.py

Trois questions, dans l'ordre :
  1. Quelle source reproduit le priceToBeat officiel ? (TWAP Chainlink, spot
     Chainlink, Binance). Écart médian proche de 0 $ = c'est la bonne.
  2. Le modèle est-il calibré ? Quand il annonce P(Up) = 70 %, Up gagne-t-il
     ~70 % du temps ? Et prédit-il mieux que le prix du marché (score de Brier
     plus bas = meilleur) ?
  3. Quel seuil MIN_EDGE aurait gagné de l'argent ? Simulation : à chaque
     fenêtre, premier point de contrôle où l'avance dépasse le seuil, achat au
     meilleur ask (frais compris), 1 part.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from outcome_recorder import OUTCOMES_FILE, SHADOW_LOG_FILE  # noqa: E402
from probability import cost_per_share  # noqa: E402

EDGE_THRESHOLDS = (0.00, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15)


def load_jsonl(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _q(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def reference_section(opens: list[dict], outcomes: dict[int, dict]) -> list[str]:
    lines = ["== 1. Quelle source reproduit le priceToBeat officiel ? =="]
    for key, label in (("twap", "TWAP Chainlink 60 s"), ("spot", "Spot Chainlink"),
                       ("binance", "Binance (USDT)")):
        errs = [o[key] - outcomes[o["window_ts"]]["price_to_beat"]
                for o in opens if o.get(key) is not None and o["window_ts"] in outcomes]
        if not errs:
            lines.append(f"  {label:20s} : pas de données")
            continue
        abs_errs = [abs(e) for e in errs]
        lines.append(f"  {label:20s} : {len(errs):4d} fenêtres | écart médian "
                     f"{statistics.median(errs):+8.2f} $ | |écart| 90 % < {_q(abs_errs, 0.9):7.2f} $")
    return lines


def brier(pairs: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def calibration_section(quotes: list[dict], outcomes: dict[int, dict]) -> list[str]:
    lines = ["", "== 2. Calibration du modèle =="]
    rows = [(q, 1 if outcomes[q["window_ts"]]["winner"] == "Up" else 0)
            for q in quotes if q["window_ts"] in outcomes and q.get("q_up") is not None]
    if not rows:
        return lines + ["  pas encore de fenêtre résolue"]
    buckets = defaultdict(list)
    for q, y in rows:
        buckets[min(9, int(q["q_up"] * 10))].append(y)
    lines.append("  P(Up) annoncée | n    | Up observé")
    for b in sorted(buckets):
        ys = buckets[b]
        lines.append(f"  {b / 10:.1f}-{(b + 1) / 10:.1f}        | {len(ys):4d} | {sum(ys) / len(ys):6.1%}")

    model, market = [], []
    for q, y in rows:
        if q.get("ask_up") and q.get("ask_down"):
            implied = q["ask_up"] / (q["ask_up"] + q["ask_down"])
            model.append((q["q_up"], y))
            market.append((implied, y))
    if model:
        lines.append(f"  Brier modèle {brier(model):.4f} vs marché {brier(market):.4f} "
                     f"sur {len(model)} cotations ({'modèle meilleur' if brier(model) < brier(market) else 'marché meilleur ou égal'})")
    return lines


def strategy_section(quotes: list[dict], outcomes: dict[int, dict]) -> list[str]:
    lines = ["", "== 3. Simulation par seuil d'avance (1 part, achat au meilleur ask, frais compris) =="]
    by_window = defaultdict(list)
    for q in quotes:
        if q["window_ts"] in outcomes:
            by_window[q["window_ts"]].append(q)
    if not by_window:
        return lines + ["  pas encore de fenêtre résolue"]
    lines.append("  seuil | trades | gagnés | P&L/part moyen | P&L total (1 part)")
    for threshold in EDGE_THRESHOLDS:
        pnls, wins = [], 0
        for ts, qs in by_window.items():
            for q in sorted(qs, key=lambda r: -r["seconds_left"]):
                choices = []
                if q.get("ask_up"):
                    choices.append((q["q_up"] - cost_per_share(q["ask_up"]), "Up", q["ask_up"]))
                if q.get("ask_down"):
                    choices.append((1 - q["q_up"] - cost_per_share(q["ask_down"]), "Down", q["ask_down"]))
                if not choices:
                    continue
                e, side, ask = max(choices)
                if e >= threshold and ask <= 0.97:
                    won = outcomes[ts]["winner"] == side
                    wins += won
                    pnls.append((1.0 if won else 0.0) - cost_per_share(ask))
                    break
        if pnls:
            lines.append(f"  {threshold:5.2f} | {len(pnls):6d} | {wins / len(pnls):6.1%} | "
                         f"{sum(pnls) / len(pnls):+14.4f} | {sum(pnls):+.2f}")
        else:
            lines.append(f"  {threshold:5.2f} |      0 |")
    return lines


def build_report(shadow: list[dict], outcome_rows: list[dict]) -> str:
    outcomes = {o["window_ts"]: o for o in outcome_rows}
    opens = [r for r in shadow if r.get("kind") == "open"]
    quotes = [r for r in shadow if r.get("kind") == "quote"]
    lines = [f"Journal : {len(opens)} ouvertures, {len(quotes)} cotations, "
             f"{len(outcomes)} fenêtres résolues"]
    lines += reference_section(opens, outcomes)
    lines += calibration_section(quotes, outcomes)
    lines += strategy_section(quotes, outcomes)
    if len(outcomes) < 300:
        lines += ["", f"Attention : {len(outcomes)} fenêtres résolues — viser au moins 300 "
                      "(environ un jour) avant de conclure."]
    return "\n".join(lines)


def main() -> None:
    print(build_report(load_jsonl(SHADOW_LOG_FILE), load_jsonl(OUTCOMES_FILE)))


if __name__ == "__main__":
    main()
