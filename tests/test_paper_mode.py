import asyncio
import dataclasses
import json

import pytest

from balance_guard import STATE_FILE
from config import BotConfig
from learning_engine import LearningEngine, TradeRecord
from paper_resolver import GIVE_UP_AFTER, PaperResolver, resolved_winner
from scripts.paper_report import breakeven, build_report
from wallet_manager import WalletManager, paper_payout

from test_sniper import make_market, make_sniper


def paper_config(balance=100.0) -> BotConfig:
    return dataclasses.replace(BotConfig.from_env(), paper_trading=True, paper_balance=balance)


# ─── Sniper en papier ─────────────────────────────────────────────────────────

def make_paper_sniper(market, balance=100.0, side="Up", confidence=0.90):
    sniper, clob, le = make_sniper(market, balance=balance, side=side, confidence=confidence)
    sniper._config = paper_config(balance)
    sniper._wm = WalletManager(sniper._config)
    return sniper, clob, le


def test_paper_trade_mirrors_live_sizing_and_price():
    market = make_market(up=0.60, down=0.40)
    sniper, clob, le = make_paper_sniper(market, side="Down")
    assert asyncio.run(sniper._attempt_entry(market)) is True
    assert clob.orders == []  # aucun ordre réel
    rec = le._history[0]
    assert rec["source"] == "paper" and rec["side"] == "Down"
    assert rec["share_price"] == pytest.approx(0.42)  # prix + ASK_BUFFER, comme en réel
    shares = rec["amount_usdc"] / rec["share_price"]
    assert shares == pytest.approx(round(shares))  # nombre entier de parts
    assert rec["amount_usdc"] <= 100.0 * 0.20 + 1e-9  # plafond du mode NORMAL
    assert asyncio.run(sniper._wm.get_usdc_balance()) == pytest.approx(100.0 - rec["amount_usdc"])


def test_paper_mode_ignores_stale_live_state_file():
    # Fichier laissé par la session réelle d'avril (RUINE) : ne doit pas bloquer le papier
    with open(STATE_FILE, "w") as f:
        json.dump({"mode": "RUINE"}, f)
    market = make_market()
    sniper, _, le = make_paper_sniper(market, balance=100.0)
    assert asyncio.run(sniper._attempt_entry(market)) is True
    assert len(le._history) == 1


def test_paper_mode_uses_simulated_balance_for_mode():
    market = make_market()
    sniper, _, le = make_paper_sniper(market, balance=2.0)  # DERNIER → aucun ordre
    assert asyncio.run(sniper._attempt_entry(market)) is False
    assert le._history == []


# ─── Paper resolver ───────────────────────────────────────────────────────────

class FakeMM:
    def __init__(self, markets):
        self.markets = markets
        self.calls = []

    async def get_market_by_window_ts(self, ts):
        self.calls.append(ts)
        return self.markets.get(ts)


def resolved_market(window_ts, winner, closed=True, resolved=True):
    m = make_market()
    m.window_ts = window_ts
    m.up_price, m.down_price = (1.0, 0.0) if winner == "Up" else (0.0, 1.0)
    m.is_closed, m.is_resolved = closed, resolved
    return m


def setup_trade(side="Up", window_ts=1_000_200, price=0.60, shares=10, source="paper"):
    le = LearningEngine()
    le.record_trade(TradeRecord(window_ts=window_ts, side=side, btc_distance=40.0,
                                seconds_remaining=90.0, confidence=0.8, share_price=price,
                                amount_usdc=round(shares * price, 2), source=source))
    wm = WalletManager(paper_config(100.0))
    wm.paper_deduct(round(shares * price, 2))
    return le, wm


AFTER = 1_000_200 + 300 + 61  # fin de fenêtre + délai de grâce


def test_resolver_records_win_and_credits_balance():
    le, wm = setup_trade(side="Up", price=0.60, shares=10)
    mm = FakeMM({1_000_200: resolved_market(1_000_200, "Up")})
    assert asyncio.run(PaperResolver(mm, le, wm).resolve_pending(now=AFTER)) == 1
    rec = le._history[0]
    assert rec["won"] is True
    assert rec["payout"] == pytest.approx(paper_payout(10, 6.0))  # 10 − 7,2 % × 4 = 9,712
    assert asyncio.run(wm.get_usdc_balance()) == pytest.approx(94.0 + 9.712)


def test_resolver_records_loss():
    le, wm = setup_trade(side="Up")
    mm = FakeMM({1_000_200: resolved_market(1_000_200, "Down")})
    asyncio.run(PaperResolver(mm, le, wm).resolve_pending(now=AFTER))
    assert le._history[0]["won"] is False and le._history[0]["payout"] == 0
    assert asyncio.run(wm.get_usdc_balance()) == pytest.approx(94.0)


def test_resolver_waits_for_window_end_and_resolution():
    le, wm = setup_trade()
    mm = FakeMM({1_000_200: resolved_market(1_000_200, "Up", closed=False, resolved=False)})
    r = PaperResolver(mm, le, wm)
    asyncio.run(r.resolve_pending(now=1_000_200 + 300 + 30))  # délai de grâce
    assert mm.calls == []
    asyncio.run(r.resolve_pending(now=AFTER))  # marché pas encore résolu
    assert le._history[0]["won"] is None


def test_resolver_gives_up_after_24h():
    le, wm = setup_trade()
    asyncio.run(PaperResolver(FakeMM({}), le, wm).resolve_pending(
        now=1_000_200 + 300 + GIVE_UP_AFTER + 1))
    assert le._history == []


def test_resolver_ignores_live_trades():
    le, wm = setup_trade(source="live")
    mm = FakeMM({1_000_200: resolved_market(1_000_200, "Up")})
    asyncio.run(PaperResolver(mm, le, wm).resolve_pending(now=AFTER))
    assert mm.calls == [] and le._history[0]["won"] is None


@pytest.mark.parametrize("up,down,closed,resolved,expected", [
    (1.0, 0.0, True, True, "Up"),
    (0.0, 1.0, True, False, "Down"),
    (0.0, 1.0, False, True, "Down"),
    (0.97, 0.03, True, True, None),   # pas un 1/0 net
    (1.0, 0.0, False, False, None),   # encore ouvert
])
def test_resolved_winner(up, down, closed, resolved, expected):
    m = make_market(up=up, down=down)
    m.is_closed, m.is_resolved = closed, resolved
    assert resolved_winner(m) == expected


# ─── Rapport ──────────────────────────────────────────────────────────────────

def test_breakeven_includes_fees():
    assert breakeven(0.50) == pytest.approx(0.5 / (0.5 + 0.5 * 0.928))
    assert breakeven(0.70) > 0.70


def test_report_numbers():
    history = [
        {"window_ts": i, "side": "Up", "confidence": 0.85, "share_price": 0.60,
         "amount_usdc": 6.0, "won": i < 7, "payout": 9.712 if i < 7 else 0.0,
         "timestamp": 1_700_000_000 + i * 300, "source": "paper"}
        for i in range(10)
    ] + [{"window_ts": 99, "side": "Up", "confidence": 0.7, "share_price": 0.5,
          "amount_usdc": 5.0, "won": None, "payout": 0.0,
          "timestamp": 1_700_010_000, "source": "paper"}]
    report = build_report(history, "paper")
    assert "7/10 (70.0%)" in report
    assert "10 résolus, 1 en attente" in report
    assert "P&L $+7.98" in report  # 7 × 9,712 − 60
    assert "OK 0.80-0.90" in report
    assert "trop peu pour conclure" in report


def test_report_filters_source():
    history = [{"window_ts": 1, "side": "Up", "confidence": 0.8, "share_price": 0.6,
                "amount_usdc": 6.0, "won": True, "payout": 9.7,
                "timestamp": 1_700_000_000, "source": "live"}]
    assert "Aucun trade." in build_report(history, "paper")
