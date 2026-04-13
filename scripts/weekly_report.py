"""
Rapport de performance hebdomadaire.
Analysé et envoyé chaque lundi à 8h EDT (12h UTC) par Perplexity Computer.
Compare la semaine actuelle à la semaine précédente.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

TRACKING_DIR = Path("/home/user/workspace/cron_tracking/f571ec9c")
TRADE_HISTORY_FILE = Path("/home/user/workspace/chatbot_polymarket/trade_history.json")
LEARNED_PARAMS_FILE = Path("/home/user/workspace/chatbot_polymarket/learned_params.json")
OUTPUT_FILE = Path("/home/user/workspace/cron_tracking/weekly_report.json")


def load_trade_history():
    if TRADE_HISTORY_FILE.exists():
        with open(TRADE_HISTORY_FILE) as f:
            return json.load(f)
    return []


def load_learned_params():
    if LEARNED_PARAMS_FILE.exists():
        with open(LEARNED_PARAMS_FILE) as f:
            return json.load(f)
    return {}


def analyze_week(trades, start: datetime, end: datetime):
    week_trades = []
    for t in trades:
        ts_str = t.get("timestamp") or t.get("resolved_at") or t.get("placed_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if start <= ts < end:
                week_trades.append(t)
        except Exception:
            continue

    total = len(week_trades)
    wins = sum(1 for t in week_trades if t.get("outcome") == "win" or t.get("result") == "win")
    losses = sum(1 for t in week_trades if t.get("outcome") == "loss" or t.get("result") == "loss")
    pending = total - wins - losses

    pnl = sum(
        float(t.get("pnl", 0) or t.get("profit", 0) or 0)
        for t in week_trades
    )

    avg_confidence = 0.0
    conf_values = [float(t.get("confidence", 0)) for t in week_trades if t.get("confidence")]
    if conf_values:
        avg_confidence = sum(conf_values) / len(conf_values)

    win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate": round(win_rate, 4),
        "pnl": round(pnl, 4),
        "avg_confidence": round(avg_confidence, 4),
    }


def main():
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    prev_week_start = now - timedelta(days=14)

    trades = load_trade_history()
    params = load_learned_params()

    current_week = analyze_week(trades, week_start, now)
    prev_week = analyze_week(trades, prev_week_start, week_start)

    # Delta comparatif
    delta_trades = current_week["total_trades"] - prev_week["total_trades"]
    delta_pnl = current_week["pnl"] - prev_week["pnl"]
    delta_winrate = current_week["win_rate"] - prev_week["win_rate"]

    # Top 3 signal directions
    direction_counts = {}
    for t in trades:
        d = t.get("direction") or t.get("signal")
        if d:
            direction_counts[d] = direction_counts.get(d, 0) + 1

    report = {
        "report_date": now.isoformat(),
        "period": f"{week_start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')}",
        "current_week": current_week,
        "prev_week": prev_week,
        "delta": {
            "trades": delta_trades,
            "pnl": round(delta_pnl, 4),
            "win_rate": round(delta_winrate, 4),
        },
        "learned_params_summary": {
            "confidence_threshold": params.get("medium_confidence_threshold", "N/A"),
            "min_distance": params.get("min_btc_distance", "N/A"),
            "total_calibrations": params.get("total_calibrations", 0),
        },
        "direction_distribution": direction_counts,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report))
    return report


if __name__ == "__main__":
    report = main()
    cw = report["current_week"]
    pw = report["prev_week"]
    d = report["delta"]

    trend_pnl = "📈" if d["pnl"] >= 0 else "📉"
    trend_wr = "📈" if d["win_rate"] >= 0 else "📉"

    notification = {
        "notify": True,
        "title": f"Rapport hebdomadaire — bot Polymarket",
        "body": (
            f"Semaine du {report['period']}:\n"
            f"• Trades: {cw['total_trades']} ({'+' if d['trades']>=0 else ''}{d['trades']} vs semaine préc.)\n"
            f"• Win rate: {cw['win_rate']*100:.1f}% {trend_wr} ({d['win_rate']*100:+.1f}%)\n"
            f"• P&L: ${cw['pnl']:+.2f} {trend_pnl} (vs ${pw['pnl']:+.2f} sem. préc.)\n"
            f"• Calibrations LearningEngine: {report['learned_params_summary']['total_calibrations']}"
        )
    }
    print("\n---NOTIFICATION---")
    print(json.dumps(notification))
