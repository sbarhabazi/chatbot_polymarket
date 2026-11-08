"""Stratégie v7 : modèle de probabilité, frais réels, flux Chainlink, journaux."""
import asyncio
import json
import math

import pytest

import price_analyzer as pa_mod
from outcome_recorder import OUTCOMES_FILE, SHADOW_LOG_FILE, OutcomeRecorder
from probability import (
    edge,
    final_distribution,
    kelly_fraction,
    prob_up,
    taker_fee,
)
from reference_feed import ReferenceFeed

from test_sniper import make_config, make_market, make_sniper


# ─── Frais et modèle ──────────────────────────────────────────────────────────

def test_taker_fee_matches_polymarket_docs():
    # Docs : 100 parts à 0,50 $ → 1,75 $ ; à 0,01 $ → 0,07 $
    assert 100 * taker_fee(0.50) == pytest.approx(1.75)
    assert 100 * taker_fee(0.01) == pytest.approx(0.0693)


def test_spot_distribution():
    mean, sd = final_distribution(100.0, 100.0, 2.0, twap=False)
    assert mean == 100.0 and sd == pytest.approx(20.0)


def test_twap_distribution_before_and_inside_window():
    _, sd_far = final_distribution(0.0, 120.0, 1.0, twap=True)
    assert sd_far == pytest.approx(math.sqrt(120 - 60 + 20))
    mean, sd = final_distribution(10.0, 30.0, 1.0, twap=True, observed_window_avg=0.0)
    assert mean == pytest.approx(5.0)  # moitié observée (0), moitié future (10)
    assert sd == pytest.approx(math.sqrt(0.25 * 30 / 3))


def test_basis_noise_widens_distribution():
    _, sd = final_distribution(0.0, 0.0, 1.0, twap=False, basis_noise=25.0)
    assert sd == pytest.approx(25.0)


def test_prob_up_bounded():
    assert prob_up(200.0, 1.0, 100.0) == 0.97
    assert prob_up(0.0, 1.0, 100.0) == 0.03
    assert prob_up(100.0, 10.0, 100.0) == pytest.approx(0.5)


def test_edge_and_kelly():
    assert edge(0.70, 0.60) == pytest.approx(0.70 - 0.60 - 0.07 * 0.6 * 0.4)
    assert kelly_fraction(0.55, 0.60) == 0.0  # espérance négative
    assert kelly_fraction(0.90, 0.60) > 0.5


# ─── Décision par valeur ──────────────────────────────────────────────────────

def test_buys_side_with_best_value_not_most_likely():
    # Modèle : P(Up) = 0,60. Up coûte 0,70 (trop cher), Down 0,25 pour P = 0,40 → Down
    market = make_market(up=0.70, down=0.25)
    sniper, clob, le = make_sniper(market, side="Up", confidence=0.60)
    assert asyncio.run(sniper._attempt_entry(market)) is True
    assert clob.orders[0].token_id == "DOWN"
    assert le._history[0]["confidence"] == pytest.approx(0.40, abs=0.02)


def test_skips_when_edge_below_minimum():
    # P(Up) = 0,62 mais Up coûte 0,60 (+ 0,02 de marge + frais) : pas assez d'avance
    market = make_market(up=0.60, down=0.45)
    sniper, clob, le = make_sniper(market, side="Up", confidence=0.62)
    assert asyncio.run(sniper._attempt_entry(market)) is False
    assert clob.orders == [] and le._history == []


def test_skips_without_volatility():
    market = make_market()
    sniper, clob, _ = make_sniper(market)
    sniper._pa.volatility_per_sqrt_s = lambda lookback=300.0: None
    assert asyncio.run(sniper._attempt_entry(market)) is False
    assert clob.orders == []


def test_shadow_log_records_checkpoints(isolated_cwd):
    market = make_market()  # 90 s restantes → point de contrôle 90
    sniper, _, _ = make_sniper(market, side="Up", confidence=0.62)
    sniper._config = make_config(min_edge=0.99)  # aucun trade
    asyncio.run(sniper._attempt_entry(market))
    asyncio.run(sniper._attempt_entry(market))  # même point : pas de doublon
    rows = [json.loads(line) for line in open(SHADOW_LOG_FILE)]
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "quote" and r["checkpoint"] == 90
    assert r["ask_up"] == 0.60 and r["q_up"] == pytest.approx(0.62, abs=0.02)


# ─── Mode Chainlink ───────────────────────────────────────────────────────────

class FakeRef:
    def __init__(self, twap=100_000.0, spot=100_060.0, sigma=1.0):
        self.twap, self.spot, self.sigma = twap, spot, sigma

    def latest(self, kind, max_age=10.0):
        return self.spot

    def value_at(self, kind, ts, max_gap=3.0):
        return self.twap if kind == "twap" else self.spot - 1

    def volatility(self, kind="spot", lookback=300.0):
        return self.sigma

    def average(self, kind, start, end):
        return self.spot


def test_chainlink_twap_mode_uses_twap_at_open():
    market = make_market(up=0.60, down=0.40)
    sniper, clob, le = make_sniper(market)
    sniper._config = make_config(reference_feed="chainlink_twap")
    sniper._ref = FakeRef()
    sniper._window_open = None
    asyncio.run(sniper._capture_window_open(market.window_ts, 90))
    assert sniper._window_open == (market.window_ts, 100_000.0, "chainlink-twap")
    # +60 $ au-dessus, σ = 1 $/√s, 90 s restantes : Up quasi certain → achat Up
    assert asyncio.run(sniper._attempt_entry(market)) is True
    assert clob.orders[0].token_id == "UP"


def test_chainlink_mode_skips_when_feed_silent():
    market = make_market()
    sniper, clob, _ = make_sniper(market)
    sniper._config = make_config(reference_feed="chainlink")
    sniper._ref = FakeRef(spot=None)
    sniper._ref.latest = lambda kind, max_age=10.0: None
    assert asyncio.run(sniper._attempt_entry(market)) is False
    assert clob.orders == []


# ─── ReferenceFeed (format PolyBolt documenté) ────────────────────────────────

def test_reference_feed_parses_snapshot_and_updates():
    feed = ReferenceFeed({"apiKey": "k", "secret": "s", "passphrase": "p"})
    feed.handle_message(json.dumps({
        "v": 1, "channel": "price.crypto", "seq": 1, "ts": 1788973000000, "snapshot": True,
        "payload": {"symbol": "btcusd", "source": "chainlink", "data": [
            {"timestamp": 1788972880000, "value": 64123.5, "full_accuracy_value": "64123.50000000"},
            {"timestamp": 1788972881000, "value": 64125.1, "full_accuracy_value": "64125.10000000"},
        ]}}))
    feed.handle_message(json.dumps({
        "v": 1, "channel": "price.crypto.twap", "seq": 3, "ts": 1788973002000,
        "payload": {"symbol": "btcusd", "value": 64120.7, "full_accuracy_value": "64120.70000000",
                    "timestamp": 1788973002000, "window_seconds": 60, "source": "chainlink"}}))
    assert feed.value_at("spot", 1788972881.5) == pytest.approx(64125.1)
    assert feed.value_at("twap", 1788973002.0) == pytest.approx(64120.7)
    assert feed.latest("spot", now=1788972882.0) == pytest.approx(64125.1)
    assert feed.latest("spot", now=1788972999.0) is None  # périmé


def test_reference_feed_ignores_other_symbols_and_errors():
    feed = ReferenceFeed({"apiKey": "k"})
    feed.handle_message(json.dumps({"channel": "price.crypto",
                                    "payload": {"symbol": "ethusd", "value": 1, "timestamp": 1}}))
    feed.handle_message(json.dumps({"op": "error", "code": "auth_invalid", "channel": "price.crypto"}))
    feed.handle_message("pas du json")
    assert feed.value_at("spot", 10) is None and feed.last_error == "auth_invalid (price.crypto)"


def test_reference_feed_volatility_and_average():
    feed = ReferenceFeed(None)
    for i in range(200):
        feed.add_point("spot", 1000.0 + i, 100.0 + (1.0 if i % 2 else -1.0))
    assert feed.volatility("spot", 300.0, now=1200.0) == pytest.approx(2.0, rel=0.05)
    assert feed.average("spot", 1000.0, 1001.0) == pytest.approx(100.0)
    assert not feed.enabled


# ─── Résultats officiels ──────────────────────────────────────────────────────

class FakeMM:
    def __init__(self):
        self.calls = []

    async def get_window_result(self, ts):
        self.calls.append(ts)
        return {"window_ts": ts, "price_to_beat": 1.0, "final_price": 2.0, "winner": "Up"}


def test_outcome_recorder_waits_for_publication(isolated_cwd):
    with open(SHADOW_LOG_FILE, "w") as f:
        f.write(json.dumps({"kind": "quote", "window_ts": 1000}) + "\n")
    mm = FakeMM()
    rec = OutcomeRecorder(mm)
    assert asyncio.run(rec.record_pending(now=1000 + 300 + 60)) == 0 and mm.calls == []
    assert asyncio.run(rec.record_pending(now=1000 + 300 + 7 * 60 + 1)) == 1
    assert asyncio.run(rec.record_pending(now=1000 + 300 + 7 * 60 + 2)) == 0  # déjà fait
    rows = [json.loads(line) for line in open(OUTCOMES_FILE)]
    assert rows == [{"window_ts": 1000, "price_to_beat": 1.0, "final_price": 2.0, "winner": "Up"}]


# ─── Sources de prix : retour à Binance ───────────────────────────────────────

def test_falls_back_then_returns_to_binance(monkeypatch):
    monkeypatch.setattr(pa_mod, "BINANCE_RETRIES", 1)
    monkeypatch.setattr(pa_mod, "FALLBACK_MAX_SECONDS", 0.05)
    pa = pa_mod.PriceAnalyzer(make_config())
    pa._running = True
    calls = []

    async def binance():
        calls.append("Binance")
        if calls.count("Binance") >= 2:
            pa._running = False
        raise ConnectionError("down")

    async def kraken():
        calls.append("Kraken")
        await asyncio.sleep(10)  # flux vivant : coupé au bout de FALLBACK_MAX_SECONDS

    pa._consume_binance_trades = binance
    pa._consume_kraken_trades = kraken
    asyncio.run(asyncio.wait_for(pa._connect_best_trade_source(), timeout=10))
    assert calls == ["Binance", "Kraken", "Binance"]


def test_idle_stream_times_out():
    class SilentWS:
        async def recv(self):
            await asyncio.sleep(10)

    async def consume():
        async for _ in pa_mod._with_idle_timeout(SilentWS(), timeout=0.05):
            pass

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(consume())


def test_binance_volatility(monkeypatch):
    import time as _time
    from models import PriceSnapshot
    pa = pa_mod.PriceAnalyzer(make_config())
    monkeypatch.setattr(_time, "time", lambda: 2000.0)
    for i in range(200):
        pa._add_price(PriceSnapshot(price=100.0 + (2.0 if i % 2 else 0.0),
                                    timestamp=1800.0 + i, volume=1))
    assert pa.volatility_per_sqrt_s(300.0) == pytest.approx(2.0, rel=0.05)


# ─── Rapport de calibration ───────────────────────────────────────────────────

def test_shadow_report_sections():
    from scripts.shadow_report import build_report
    shadow, outcomes = [], []
    for i in range(10):
        ts = 1000 + 300 * i
        up_wins = i < 7
        shadow.append({"kind": "open", "window_ts": ts, "twap": 100.0, "spot": 101.0, "binance": 220.0})
        shadow.append({"kind": "quote", "window_ts": ts, "checkpoint": 60, "seconds_left": 60,
                       "q_up": 0.80, "ask_up": 0.60, "ask_down": 0.42})
        outcomes.append({"window_ts": ts, "price_to_beat": 100.0, "final_price": 101.0 if up_wins else 99.0,
                         "winner": "Up" if up_wins else "Down"})
    report = build_report(shadow, outcomes)
    assert "TWAP Chainlink 60 s  :   10 fenêtres | écart médian    +0.00 $" in report
    assert "Binance (USDT)" in report and "+120.00 $" in report
    assert "0.8-0.9        |   10 |  70.0%" in report
    # edge = 0,80 − (0,60 + 0,07×0,6×0,4) = 0,183 : tradé jusqu'au seuil 0,15
    assert "  0.15 |     10 |  70.0% |" in report
    assert "viser au moins 300" in report


def test_shadow_report_empty():
    from scripts.shadow_report import build_report
    assert "pas encore de fenêtre résolue" in build_report([], [])
