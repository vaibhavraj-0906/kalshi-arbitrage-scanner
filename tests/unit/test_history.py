from __future__ import annotations

from karb.history import minute_snapshots, screen_history
from karb.structure.classify import classify_event
from karb.wire.candles import CandleWire
from tests.support import SERIES, event, market


def candle(ts: int, bid: str, ask: str) -> CandleWire:
    return CandleWire.model_validate(
        {
            "end_period_ts": ts,
            "yes_bid": {
                "open_dollars": bid,
                "low_dollars": bid,
                "high_dollars": bid,
                "close_dollars": bid,
            },
            "yes_ask": {
                "open_dollars": ask,
                "low_dollars": ask,
                "high_dollars": ask,
                "close_dollars": ask,
            },
        }
    )


def test_quiet_minutes_carry_the_last_close_forward() -> None:
    candles = {
        "A": [candle(60, "0.4000", "0.4500"), candle(180, "0.4100", "0.4600")],
        "B": [candle(120, "0.3000", "0.3500")],
    }
    snapshots = list(minute_snapshots(candles))
    assert [snapshot.ts for snapshot in snapshots] == [60, 120, 180]
    assert "B" not in snapshots[0].quotes
    assert str(snapshots[1].quotes["A"].yes_bid) == "0.4000"  # carried from minute 60
    assert str(snapshots[2].quotes["B"].yes_ask) == "0.3500"  # carried from minute 120


def test_candles_encode_missing_quotes_as_zero_bid_and_one_dollar_ask() -> None:
    (snapshot,) = minute_snapshots({"A": [candle(60, "0.0000", "1.0000")]})
    assert snapshot.quotes["A"].yes_bid is None
    assert snapshot.quotes["A"].yes_ask is None


def test_candles_before_the_start_seed_quotes_without_a_snapshot() -> None:
    candles = {"A": [candle(60, "0.4000", "0.4500")], "B": [candle(120, "0.3000", "0.3500")]}
    snapshots = list(minute_snapshots(candles, start_ts=120))
    assert [snapshot.ts for snapshot in snapshots] == [120]
    assert set(snapshots[0].quotes) == {"A", "B"}


def test_history_screen_counts_minutes_with_violations() -> None:
    structure = classify_event(
        event([market("A"), market("B"), market("C")], mutually_exclusive=True), SERIES
    ).structure
    assert structure is not None
    candles = {
        "A": [candle(60, "0.3000", "0.3500"), candle(120, "0.4500", "0.5000")],
        "B": [candle(60, "0.3000", "0.3500"), candle(120, "0.4000", "0.4500")],
        "C": [candle(60, "0.3000", "0.3500")],
    }
    result = screen_history(structure, minute_snapshots(candles))
    assert (result.minutes, result.minutes_with_hits) == (2, 1)  # minute 120: 0.45 + 0.40 + 0.30
    assert result.hits_by_rule == {"LOGICAL: bids over $1": 1}
    assert str(result.best_edge["LOGICAL: bids over $1"]) == "0.1500"
    assert (result.first_ts, result.last_ts) == (60, 120)
