"""Opt-in smoke test of candlestick history against the public Kalshi API (uv run pytest -m live)."""

from __future__ import annotations

import pytest

from karb.exchange.client import KalshiClient
from karb.exchange.endpoints import iter_events
from karb.history import fetch_candles, minute_snapshots

pytestmark = pytest.mark.live


async def test_candles_for_a_live_threshold_ladder() -> None:
    async with KalshiClient() as client:
        events = [event async for event in iter_events(client, series_ticker="KXBTCD", max_pages=1)]
        assert events, "no open BTC events"
        tickers = sorted(events[0].tickers)[:3]
        end = int(client.clock.now().timestamp())
        candles = await fetch_candles(client, tickers, start_ts=end - 3600, end_ts=end)
        assert set(tickers) <= set(candles)
        for series in candles.values():
            ends = [candle.end_period_ts for candle in series]
            assert ends == sorted(set(ends))
        assert all(snapshot.ts <= end + 60 for snapshot in minute_snapshots(candles))
