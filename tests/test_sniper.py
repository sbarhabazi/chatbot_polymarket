import asyncio
import dataclasses
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from config import BotConfig
from learning_engine import LearningEngine
from market_manager import compute_window_timestamp
from models import MarketInfo
from sniper import Sniper


def make_config(**overrides) -> BotConfig:
    cfg = BotConfig.from_env()
    return dataclasses.replace(cfg, paper_trading=False, **overrides)


def make_market(up=0.60, down=0.40, best_ask=0.0) -> MarketInfo:
    now = datetime.now(timezone.utc)
    return MarketInfo(
        condition_id="0xabc", up_token_id="UP", down_token_id="DOWN",
        end_time=now + timedelta(seconds=90), start_time=now - timedelta(seconds=210),
        slug="btc-updown-5m-test", window_ts=compute_window_timestamp(),
        up_price=up, down_price=down, liquidity=10_000, best_ask=best_ask,
        price_to_beat=100_000.0,
    )


class FakeMarketManager:
    def __init__(self, market):
        self.market = market

    async def get_market_by_window_ts(self, _ts):
        return self.market

    def seconds_remaining(self, market):
        return (market.end_time - datetime.now(timezone.utc)).total_seconds()


class FakePriceAnalyzer:
    is_connected = True

    def __init__(self, side="Up", confidence=0.90):
        self.side = side
        self.confidence = confidence

    def compute_distance_signal(self, price_to_beat, seconds_remaining):
        return SimpleNamespace(
            recommended_side=self.side, confidence=self.confidence,
            distance_confidence=self.confidence, skip_reason=None,
            signal_agreement=3, signal_total=4, btc_distance=60.0,
            obi=0.0, rsi=50.0, current_price=price_to_beat + 60.0,
        )


class FakeWallet:
    def __init__(self, balance):
        self.balance = balance

    async def get_usdc_balance(self, force_refresh=False):
        return self.balance

    def invalidate_cache(self):
        pass


class FakeClob:
    def __init__(self, status="matched", delay=0.0, raises=None):
        self.status = status
        self.delay = delay
        self.raises = raises
        self.orders = []

    def create_order(self, args):
        time.sleep(self.delay)  # appel HTTP synchrone simulé
        self.orders.append(args)
        return args

    def post_order(self, signed, order_type):
        time.sleep(self.delay)
        if self.raises:
            raise Exception(self.raises)
        return {"status": self.status, "makingAmount": "", "takingAmount": "",
                "orderID": "0xorder"}


def make_sniper(market, *, balance=100.0, side="Up", confidence=0.90, clob=None):
    clob = clob or FakeClob()
    le = LearningEngine()
    sniper = Sniper(make_config(), FakeMarketManager(market), FakePriceAnalyzer(side, confidence),
                    FakeWallet(balance), clob_client=clob, learning_engine=le)
    return sniper, clob, le


def run(coro):
    return asyncio.run(coro)


def test_no_flip_when_recommended_side_unaffordable():
    # Up recommandé à $0.85 ; 5 parts = $4.25+ > cap 20 % de $10.
    # Down ($0.15) est abordable, mais le bot ne doit plus basculer dessus.
    market = make_market(up=0.85, down=0.15)
    sniper, clob, le = make_sniper(market, balance=10.0)
    assert run(sniper._attempt_entry(market)) is False
    assert clob.orders == []
    assert le._history == []


def test_order_and_learning_record_use_recommended_side():
    market = make_market(up=0.60, down=0.40)
    sniper, clob, le = make_sniper(market, balance=100.0, side="Down")
    assert run(sniper._attempt_entry(market)) is True
    assert [o.token_id for o in clob.orders] == ["DOWN"]
    assert le._history[0]["side"] == "Down"


def test_order_cost_respects_mode_cap():
    market = make_market(up=0.60, down=0.40)
    sniper, clob, le = make_sniper(market, balance=100.0, confidence=0.98)
    assert run(sniper._attempt_entry(market)) is True
    order = clob.orders[0]
    assert order.size * order.price <= 100.0 * 0.20 + 1e-9  # cap du mode NORMAL
    rec = le._history[0]
    assert rec["amount_usdc"] == pytest.approx(order.size * order.price, abs=0.01)
    assert rec["share_price"] == order.price


def test_down_order_does_not_use_up_best_ask():
    # bestAsk de Gamma = celui du token Up ($0.62) : ne pas l'offrir pour Down ($0.40)
    market = make_market(up=0.60, down=0.40, best_ask=0.62)
    sniper, clob, _ = make_sniper(market, side="Down")
    assert run(sniper._attempt_entry(market)) is True
    assert clob.orders[0].price == pytest.approx(0.42)


@pytest.mark.parametrize("clob", [
    FakeClob(status="unmatched"),
    FakeClob(raises="not enough balance / allowance"),
    FakeClob(raises="order couldn't be fully filled. FOK orders are fully filled or killed."),
])
def test_failed_order_is_not_recorded(clob):
    market = make_market()
    sniper, _, le = make_sniper(market, clob=clob)
    assert run(sniper._attempt_entry(market)) is False
    assert le._history == []
    assert sniper._order_attempted_this_window is True  # pas de nouvel essai


def test_low_balance_modes_place_no_order(isolated_cwd):
    import json
    from balance_guard import STATE_FILE
    with open(STATE_FILE, "w") as f:
        json.dump({"mode": "DERNIER", "kelly_cap": 1.0}, f)
    market = make_market()
    sniper, clob, le = make_sniper(market, balance=2.0, confidence=0.98)
    assert run(sniper._attempt_entry(market)) is False
    assert clob.orders == [] and le._history == []


def test_order_placement_does_not_block_event_loop():
    market = make_market()
    sniper, _, _ = make_sniper(market, clob=FakeClob(delay=0.3))

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        ok = await sniper._attempt_entry(market)
        t.cancel()
        return ok, ticks

    ok, ticks = run(scenario())
    assert ok is True
    # 0.6 s d'appels synchrones : la boucle doit avoir continué à tourner
    assert ticks >= 10
