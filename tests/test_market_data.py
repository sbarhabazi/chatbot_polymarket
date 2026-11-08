"""Prix réels : carnet CLOB, prix d'ouverture Binance de la fenêtre, historique par durée."""
import asyncio

import pytest

from models import OrderBook, PriceSnapshot
from price_analyzer import PRICE_RETENTION_SECONDS, PriceAnalyzer
from test_paper_mode import make_paper_sniper
from test_sniper import make_config, make_market, make_sniper


# ─── Carnet d'ordres ──────────────────────────────────────────────────────────

class TestOrderBook:
    book = OrderBook(asks=[(0.40, 10), (0.41, 5), (0.45, 100)])

    def test_best_ask(self):
        assert self.book.best_ask == 0.40
        assert OrderBook().best_ask is None

    def test_fill_walks_levels_up_to_limit(self):
        assert self.book.fill_cost(12, 0.42) == pytest.approx(10 * 0.40 + 2 * 0.41)

    def test_fill_impossible_within_limit(self):
        assert self.book.fill_cost(20, 0.42) is None  # 15 parts seulement ≤ 0,42 $


def test_paper_skips_when_book_too_thin():
    market = make_market(up=0.60, down=0.40)
    sniper, _, le = make_paper_sniper(market, side="Down")
    sniper._mm.books = {"DOWN": OrderBook(asks=[(0.40, 6.0), (0.50, 1000.0)])}
    assert asyncio.run(sniper._attempt_entry(market)) is False
    assert le._history == []


def test_paper_fill_price_is_book_average():
    market = make_market(up=0.60, down=0.40)
    sniper, _, le = make_paper_sniper(market, side="Down")
    sniper._mm.books = {"DOWN": OrderBook(asks=[(0.40, 20.0), (0.41, 1000.0)])}
    assert asyncio.run(sniper._attempt_entry(market)) is True
    rec = le._history[0]
    shares = rec["shares"]
    fill = 20 * 0.40 + (shares - 20) * 0.41
    avg = fill / shares
    assert rec["share_price"] == pytest.approx(avg)
    assert rec["amount_usdc"] == pytest.approx(round(fill + shares * 0.07 * avg * (1 - avg), 2))


def test_live_order_priced_from_book_ask():
    market = make_market(up=0.60, down=0.40)
    sniper, clob, _ = make_sniper(market, side="Up",
                                  books={"UP": OrderBook(asks=[(0.33, 1000.0)])})
    assert asyncio.run(sniper._attempt_entry(market)) is True
    assert clob.orders[0].price == pytest.approx(0.35)  # 0,33 + ASK_BUFFER


def test_skip_when_book_empty():
    market = make_market()
    sniper, clob, le = make_sniper(market, books={"UP": OrderBook()})
    assert asyncio.run(sniper._attempt_entry(market)) is False
    assert clob.orders == [] and le._history == []


def test_paper_stake_not_compounded():
    # Solde papier gonflé à 10 000 $ : la mise reste calculée sur PAPER_BALANCE (100 $)
    market = make_market(up=0.60, down=0.40)
    sniper, _, le = make_paper_sniper(market, balance=100.0)
    sniper._wm.paper_add(9_900.0)
    assert asyncio.run(sniper._attempt_entry(market)) is True
    assert le._history[0]["amount_usdc"] <= 100.0 * 0.20 + 1e-9


# ─── Prix d'ouverture de la fenêtre ───────────────────────────────────────────

def test_skip_without_window_open_price():
    market = make_market()
    sniper, clob, _ = make_sniper(market)
    sniper._window_open = None
    assert asyncio.run(sniper._attempt_entry(market)) is False
    assert clob.orders == []


def test_skip_when_price_source_changed():
    market = make_market()
    sniper, clob, _ = make_sniper(market)
    sniper._window_open = (market.window_ts, 100_000.0, "Kraken")  # source actuelle : Binance
    assert asyncio.run(sniper._attempt_entry(market)) is False
    assert clob.orders == []


class FakePA:
    def __init__(self, history_price=None, rest_price=None, source="Binance"):
        self.history_price, self.rest_price, self.active_source = history_price, rest_price, source
        self.rest_calls = 0

    def price_at(self, ts):
        return self.history_price

    async def fetch_binance_price_at(self, ts):
        self.rest_calls += 1
        return self.rest_price


@pytest.mark.parametrize("pa,expected,rest_calls", [
    (FakePA(history_price=100.0, rest_price=999.0), 100.0, 0),   # historique d'abord
    (FakePA(history_price=None, rest_price=101.0), 101.0, 1),    # démarrage en cours de fenêtre
    (FakePA(history_price=None, rest_price=None), None, 1),      # rien de fiable : fenêtre sautée
    (FakePA(history_price=None, rest_price=101.0, source="Kraken"), None, 0),  # pas d'API Kraken
])
def test_capture_window_open(pa, expected, rest_calls):
    sniper, _, _ = make_sniper(make_market())
    sniper._pa = pa
    sniper._window_open = None
    asyncio.run(sniper._capture_window_open(1_000_200, 250))
    got = sniper._window_open[1] if sniper._window_open else None
    assert got == expected and pa.rest_calls == rest_calls
    if expected:
        assert sniper._window_open == (1_000_200, expected, pa.active_source)


# ─── Historique des transactions par durée ────────────────────────────────────

def _feed(pa, start, seconds, per_second, price=100.0):
    for i in range(seconds * per_second):
        pa._add_price(PriceSnapshot(price=price + i * 0.01, timestamp=start + i / per_second,
                                    volume=0.1))


def test_buffer_keeps_time_not_count():
    pa = PriceAnalyzer(make_config())
    _feed(pa, 1_000.0, 60, 200)  # 12 000 transactions en 60 s (BTC agité)
    assert len(pa._prices) == 12_000  # l'ancien plafond de 1 200 en gardait 6 s
    assert pa._prices[-1].timestamp - pa._prices[0].timestamp > 59


def test_buffer_prunes_older_than_retention():
    pa = PriceAnalyzer(make_config())
    _feed(pa, 1_000.0, PRICE_RETENTION_SECONDS + 100, 1)
    assert pa._prices[-1].timestamp - pa._prices[0].timestamp <= PRICE_RETENTION_SECONDS


def test_price_at():
    pa = PriceAnalyzer(make_config())
    for t, p in [(100.0, 1.0), (101.0, 2.0), (103.0, 3.0)]:
        pa._add_price(PriceSnapshot(price=p, timestamp=t, volume=1))
    assert pa.price_at(102.0) == 2.0      # dernière transaction avant
    assert pa.price_at(99.0) is None      # avant l'historique
    assert pa.price_at(109.0) is None     # trou de plus de 5 s
    assert pa.price_at(103.0) == 3.0


def test_rsi_computed_during_busy_market(monkeypatch):
    import time as _time
    pa = PriceAnalyzer(make_config())
    now = 10_000.0
    monkeypatch.setattr(_time, "time", lambda: now)
    _feed(pa, now - 30, 30, 200)  # hausse continue, 6 000 transactions en 30 s
    assert pa.compute_rsi(30.0) == 100.0  # avant : 50 (données « insuffisantes »)
