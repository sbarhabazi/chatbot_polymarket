"""
Rapport mensuel comparatif du bot Polymarket.
Analyse les 30 derniers jours vs les 30 jours précédents.
Exécuté le 1er de chaque mois à 8h EDT (12h UTC) par Perplexity Computer.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

TRADE_HISTORY_FILE = Path("/home/user/workspace/chatbot_polymarket/trade_history.json")
LEARNED_PARAMS_FILE = Path("/home/user/workspace/chatbot_polymarket/learned_params.json")
MONTHLY_ARCHIVE_DIR = Path("/home/user/workspace/cron_tracking/monthly_reports")
OUTPUT_FILE = Path("/home/user/workspace/cron_tracking/monthly_report_latest.json")


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


def analyze_period(trades, start: datetime, end: datetime) -> dict:
    period_trades = []
    for t in trades:
        ts_str = t.get("timestamp") or t.get("resolved_at") or t.get("placed_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if start <= ts < end:
                period_trades.append(t)
        except Exception:
            continue

    total = len(period_trades)
    wins = sum(1 for t in period_trades if t.get("outcome") == "win" or t.get("result") == "win")
    losses = sum(1 for t in period_trades if t.get("outcome") == "loss" or t.get("result") == "loss")

    pnl = sum(float(t.get("pnl", 0) or t.get("profit", 0) or 0) for t in period_trades)

    # Meilleur et pire trade
    resolved = [
        t for t in period_trades
        if (t.get("outcome") in ("win", "loss") or t.get("result") in ("win", "loss"))
        and (t.get("pnl") is not None or t.get("profit") is not None)
    ]
    best_trade = max(resolved, key=lambda t: float(t.get("pnl", 0) or t.get("profit", 0) or 0), default=None)
    worst_trade = min(resolved, key=lambda t: float(t.get("pnl", 0) or t.get("profit", 0) or 0), default=None)

    conf_values = [float(t.get("confidence", 0)) for t in period_trades if t.get("confidence")]
    avg_confidence = round(sum(conf_values) / len(conf_values), 4) if conf_values else 0.0

    win_rate = round(wins / (wins + losses), 4) if (wins + losses) > 0 else 0.0

    # Distribution par heure (UTC)
    hourly = {}
    for t in period_trades:
        ts_str = t.get("timestamp") or t.get("placed_at")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                h = ts.hour
                hourly[h] = hourly.get(h, 0) + 1
            except Exception:
                pass

    best_hour = max(hourly, key=hourly.get) if hourly else None

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "pnl": round(pnl, 4),
        "avg_confidence": avg_confidence,
        "best_trade_pnl": round(float(best_trade.get("pnl", 0) or best_trade.get("profit", 0) or 0), 4) if best_trade else 0,
        "worst_trade_pnl": round(float(worst_trade.get("pnl", 0) or worst_trade.get("profit", 0) or 0), 4) if worst_trade else 0,
        "best_trading_hour_utc": best_hour,
        "hourly_distribution": hourly,
    }


def main():
    now = datetime.now(timezone.utc)
    month_start = now - timedelta(days=30)
    prev_month_start = now - timedelta(days=60)

    trades = load_trade_history()
    params = load_learned_params()

    current_month = analyze_period(trades, month_start, now)
    prev_month = analyze_period(trades, prev_month_start, month_start)

    delta_pnl = current_month["pnl"] - prev_month["pnl"]
    delta_wr = current_month["win_rate"] - prev_month["win_rate"]
    delta_trades = current_month["total_trades"] - prev_month["total_trades"]

    # ROI estimé (basé sur PnL / capital initial approximatif)
    roi = (current_month["pnl"] / 24.49 * 100) if current_month["total_trades"] > 0 else 0.0

    report = {
        "report_date": now.isoformat(),
        "period": f"{month_start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')}",
        "current_month": current_month,
        "prev_month": prev_month,
        "delta": {
            "trades": delta_trades,
            "pnl": round(delta_pnl, 4),
            "win_rate": round(delta_wr, 4),
        },
        "estimated_monthly_roi_pct": round(roi, 2),
        "learned_params": {
            "confidence_threshold": params.get("medium_confidence_threshold"),
            "min_btc_distance": params.get("min_btc_distance"),
            "total_calibrations": params.get("total_calibrations", 0),
        },
        "total_trades_all_time": len(trades),
    }

    # Archiver
    MONTHLY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_file = MONTHLY_ARCHIVE_DIR / f"report_{now.strftime('%Y_%m')}.json"
    with open(archive_file, "w") as f:
        json.dump(report, f, indent=2)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report))
    return report


if __name__ == "__main__":
    report = main()
    cm = report["current_month"]
    pm = report["prev_month"]
    d = report["delta"]

    trend = "📈" if d["pnl"] >= 0 else "📉"

    print("\n---NOTIFICATION---")
    print(json.dumps({
        "notify": True,
        "title": f"Rapport mensuel bot Polymarket — {datetime.now(timezone.utc).strftime('%B %Y')}",
        "body": (
            f"Bilan des 30 derniers jours:\n"
            f"• Trades: {cm['total_trades']} ({'+' if d['trades']>=0 else ''}{d['trades']} vs mois préc.)\n"
            f"• Win rate: {cm['win_rate']*100:.1f}% ({d['win_rate']*100:+.1f}%)\n"
            f"• P&L: ${cm['pnl']:+.2f} {trend} (ROI estimé: {report['estimated_monthly_roi_pct']:+.1f}%)\n"
            f"• Meilleur trade: +${cm['best_trade_pnl']:.2f} | Pire trade: ${cm['worst_trade_pnl']:.2f}\n"
            f"• Total historique: {report['total_trades_all_time']} trades, {report['learned_params']['total_calibrations']} calibrations"
        )
    }))
