from learning_engine import LearningEngine, TradeRecord


def _trade(window_ts: int) -> TradeRecord:
    return TradeRecord(window_ts=window_ts, side="Up", btc_distance=40.0,
                       seconds_remaining=90.0, confidence=0.8, share_price=0.6,
                       amount_usdc=3.0)


def test_discard_pending_trade():
    le = LearningEngine()
    le.record_trade(_trade(100))
    assert le.has_pending_trade(100)
    assert le.discard_pending_trade(100) is True
    assert not le.has_pending_trade(100)
    # Persisté sur disque
    assert LearningEngine()._history == []


def test_discard_never_removes_a_resolved_trade():
    le = LearningEngine()
    le.record_trade(_trade(100))
    le.record_result(100, won=True, payout=5.0)
    assert le.discard_pending_trade(100) is False
    assert len(le._history) == 1 and le._history[0]["won"] is True
