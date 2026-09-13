"""Opt-in smoke tests against the public Kalshi API.

    uv run pytest -m live -q

Deselected by default so CI and the offline suite never depend on Kalshi being reachable.
"""

from __future__ import annotations

import pytest

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.exchange.client import KalshiClient
from karb.exchange.endpoints import (
    fetch_exchange_status,
    fetch_orderbooks,
    fetch_series,
    iter_events,
    trading_shards,
)
from karb.structure.classify import classify_event, tradeable_tickers

pytestmark = pytest.mark.live


async def test_public_market_data_round_trip() -> None:
    async with KalshiClient() as client:
        shards = trading_shards(await fetch_exchange_status(client))
        series = await fetch_series(client)
        assert len(series) > 1_000

        classified = None
        async for event in iter_events(client, max_pages=2):
            result = classify_event(event, series.get(event.series_ticker))
            if result.structure is not None:
                classified = result.structure
                break
        assert classified is not None, "no event with exploitable structure in two pages"

        event = classified.event
        tickers = sorted(event.tickers)[:100]
        batch = await fetch_orderbooks(client, tickers, depth=10)
        assert set(batch.books) == set(tickers)
        assert batch.problems == ()

        tradeable = tradeable_tickers(event, now=client.clock.now(), trading_shards=shards)
        snapshot = EventSnapshot(
            classified, batch.books, tradeable & set(tickers), client.clock.now()
        )
        detection = detect(snapshot, DetectConfig())
        assert all(opportunity.guaranteed_pnl.raw > 0 for opportunity in detection.opportunities)
