import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest

from harvester import Harvester
from learning_engine import DEFAULT_CONFIDENCE_TABLE, LearningEngine, TradeRecord
from wallet_manager import compute_offer_price

from test_sniper import FakeClob, make_config, make_market, make_sniper


# ─── Prix offert (ASK_BUFFER) ─────────────────────────────────────────────────

class TestOfferPrice:
    def test_buffer_above_best_ask(self):
        assert compute_offer_price(0.55, 0.58, 0.02, 0.90) == pytest.approx(0.60)

    def test_market_price_when_no_ask(self):
        assert compute_offer_price(0.40, 0.0, 0.02, 0.90) == pytest.approx(0.42)

    def test_buffer_is_configurable(self):
        assert compute_offer_price(0.40, 0.0, 0.05, 0.90) == pytest.approx(0.45)

    def test_capped_at_max_buy_price(self):
        assert compute_offer_price(0.89, 0.89, 0.02, 0.90) == pytest.approx(0.90)

    def test_sniper_applies_ask_buffer(self):
        market = make_market(up=0.60, down=0.40)
        cfg = dataclasses.replace(make_config(), ask_buffer=0.05)
        sniper, clob, _ = make_sniper(market, side="Down")
        sniper._config = cfg
        assert asyncio.run(sniper._attempt_entry(market)) is True
        assert clob.orders[0].price == pytest.approx(0.45)


# ─── Table de confiance (clés int ou str) ─────────────────────────────────────

def test_get_confidence_with_in_memory_int_keys():
    le = LearningEngine()
    assert le.get_confidence(60, 120) == DEFAULT_CONFIDENCE_TABLE["50+"][120]
    assert le.get_confidence(60, 50) == pytest.approx(0.85)  # entre 60 s et 40 s


def test_get_confidence_after_json_round_trip():
    le = LearningEngine()
    le._params["confidence_table"] = json.loads(json.dumps(DEFAULT_CONFIDENCE_TABLE))
    assert le.get_confidence(25, 10) == DEFAULT_CONFIDENCE_TABLE["20-30"][10]


# ─── Moniteur GTC ─────────────────────────────────────────────────────────────

class CancelClob(FakeClob):
    def __init__(self):
        super().__init__(status="live")
        self.cancelled = []

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        return {"canceled": [order_id]}


def _gtc_sniper(btc_price):
    market = make_market()
    clob = CancelClob()
    sniper, _, _ = make_sniper(market, clob=clob)
    sniper.GTC_CHECK_INTERVAL = 0.01
    sniper.GTC_FINAL_CANCEL_SECONDS = 0.2
    sniper._pa.get_current_price = lambda: btc_price
    sniper._active_gtc_order_id = "0xorder"
    sniper._active_gtc_context = ("Up", 100_000.0)
    return sniper, clob


def test_gtc_cancelled_when_btc_reverses():
    sniper, clob = _gtc_sniper(btc_price=99_950.0)  # 50 $ sous le priceToBeat, pari Up
    loop = asyncio.new_event_loop()
    try:
        t0 = loop.time()
        loop.run_until_complete(sniper._monitor_gtc_order(1.0))
        # annulé dès le premier contrôle, puis attente de la fin de fenêtre
        assert clob.cancelled == ["0xorder"]
        assert loop.time() - t0 >= 0.95
    finally:
        loop.close()


def test_gtc_kept_while_btc_on_our_side_then_cancelled_near_close():
    sniper, clob = _gtc_sniper(btc_price=100_040.0)
    cancel_times = []
    original = sniper._cancel_order

    async def timed_cancel(order_id):
        cancel_times.append(asyncio.get_running_loop().time())
        return await original(order_id)
    sniper._cancel_order = timed_cancel

    loop = asyncio.new_event_loop()
    try:
        t0 = loop.time()
        loop.run_until_complete(sniper._monitor_gtc_order(1.0))
    finally:
        loop.close()
    # Pas d'annulation pour retournement : seulement celle de fin de fenêtre
    assert clob.cancelled == ["0xorder"]
    assert cancel_times[0] - t0 >= 0.75


# ─── Harvester ────────────────────────────────────────────────────────────────

class FakeCall:
    def __init__(self, fn):
        self.call = fn


class FakeCTF:
    def __init__(self, balances, numerators, denominator, fail=False):
        self._balances = balances
        self._numerators = numerators
        self._denominator = denominator
        self._fail = fail
        self.functions = self

    def balanceOf(self, wallet, token_id):
        def call():
            if self._fail:
                raise RuntimeError("RPC down")
            return self._balances[token_id]
        return FakeCall(call)

    def payoutDenominator(self, cond):
        return FakeCall(lambda: self._denominator)

    def payoutNumerators(self, cond, index):
        return FakeCall(lambda: self._numerators[index])


class FakeWM:
    def invalidate_cache(self):
        pass

    async def get_usdc_balance(self, force_refresh=False):
        # Solde qui bouge pendant le redeem (ordre rempli en parallèle) :
        # ne doit plus influencer le gain enregistré.
        return 999.0


def make_harvester(ctf, market, le, redeem_ok=True):
    h = Harvester.__new__(Harvester)
    h._config = make_config()
    h._mm = SimpleNamespace(get_recent_resolved_markets=lambda: _resolved(market))
    h._wm = FakeWM()
    h._redeemed = set()
    h._le = le
    h._ctf_contract = ctf

    async def redeem(_m):
        return redeem_ok
    h._redeem_position = redeem
    return h


async def _resolved(market):
    return [market]


def _market():
    m = make_market()
    m.up_token_id, m.down_token_id = "1", "2"
    m.condition_id = "0x" + "11" * 32
    return m


def _pending(le, window_ts):
    le.record_trade(TradeRecord(window_ts=window_ts, side="Up", btc_distance=40,
                                seconds_remaining=90, confidence=0.8,
                                share_price=0.6, amount_usdc=3.0))


def test_harvester_payout_from_chain_not_balance_diff():
    m, le = _market(), LearningEngine()
    _pending(le, m.window_ts)
    ctf = FakeCTF({1: 5_000_000, 2: 0}, numerators=[1, 0], denominator=1)  # Up gagne
    asyncio.run(make_harvester(ctf, m, le)._harvest_cycle())
    rec = le._history[0]
    assert rec["won"] is True and rec["payout"] == pytest.approx(5.0)


def test_harvester_records_loss_with_zero_payout():
    m, le = _market(), LearningEngine()
    _pending(le, m.window_ts)
    ctf = FakeCTF({1: 5_000_000, 2: 0}, numerators=[0, 1], denominator=1)  # Down gagne
    asyncio.run(make_harvester(ctf, m, le)._harvest_cycle())
    assert le._history[0]["won"] is False and le._history[0]["payout"] == 0


def test_harvester_waits_until_condition_settled():
    m, le = _market(), LearningEngine()
    _pending(le, m.window_ts)
    ctf = FakeCTF({1: 5_000_000, 2: 0}, numerators=[0, 0], denominator=0)
    h = make_harvester(ctf, m, le)
    asyncio.run(h._harvest_cycle())
    assert le._history[0]["won"] is None and m.condition_id not in h._redeemed


def test_harvester_discards_never_filled_trade():
    m, le = _market(), LearningEngine()
    _pending(le, m.window_ts)
    ctf = FakeCTF({1: 0, 2: 0}, numerators=[1, 0], denominator=1)
    asyncio.run(make_harvester(ctf, m, le)._harvest_cycle())
    assert le._history == []


def test_harvester_read_failure_changes_nothing():
    m, le = _market(), LearningEngine()
    _pending(le, m.window_ts)
    ctf = FakeCTF({}, numerators=[1, 0], denominator=1, fail=True)
    h = make_harvester(ctf, m, le)
    asyncio.run(h._harvest_cycle())
    assert le.has_pending_trade(m.window_ts) and not h._redeemed


# ─── Fichiers d'état dans DATA_DIR ────────────────────────────────────────────

def test_state_files_follow_data_dir(tmp_path, monkeypatch):
    import importlib

    import balance_guard
    import learning_engine
    import live_optimizer
    import paths
    import profit_taker

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("LEARNED_PARAMS_FILE", raising=False)
    monkeypatch.delenv("TRADE_HISTORY_FILE", raising=False)
    try:
        for mod in (paths, learning_engine, live_optimizer, balance_guard, profit_taker):
            importlib.reload(mod)
        data = str(tmp_path / "data")
        assert learning_engine.TRADE_HISTORY_FILE.startswith(data)
        assert learning_engine.LEARNED_PARAMS_FILE.startswith(data)
        assert str(live_optimizer.TRADE_HISTORY_FILE).startswith(data)
        assert balance_guard.STATE_FILE.startswith(data)
        assert profit_taker.PROFIT_HISTORY_FILE.startswith(data)
        assert paths.HEARTBEAT_FILE.startswith(data)
    finally:
        monkeypatch.delenv("DATA_DIR")
        for mod in (paths, learning_engine, live_optimizer, balance_guard, profit_taker):
            importlib.reload(mod)


def test_security_script_never_pushes():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "check_security.py").read_text()
    assert '"push"' not in src and '"add", "-A"' not in src
