"""The whole scanner -- discovery, screening, confirmation, tracking -- over recorded exchange data.

A mock transport serves the golden fixtures exactly as the live endpoints returned them, so
this runs every tier end to end without a network.
"""

from __future__ import annotations

import json
import random
from typing import Any

import httpx

from karb.core.clock import FakeClock
from karb.exchange.client import KalshiClient
from karb.scanner.service import ScanConfig, Scanner
from karb.structure.classify import Exclusion
from tests.support import FIXTURES, captured_at

BASE = "https://kalshi.test/trade-api/v2"
ELIGIBLE = {"KXINX-26SEP14H1600", "KXBTCD-26SEP1304", "KXNEXTDNCCHAIR-45"}


def raw(name: str) -> Any:
    return json.loads((FIXTURES / name).read_bytes())


class RecordedExchange:
    def __init__(self) -> None:
        self.events = [
            raw("events_KXINX.json")["events"][0],
            raw("events_KXBTCD.json")["events"][0],
            raw("event_KXNEXTDNCCHAIR-45.json")["event"],
            raw("event_KXELONMARS-99.json")["event"],
        ]
        self.books = {
            entry["ticker"]: entry
            for name in (
                "orderbooks_KXINX.json",
                "orderbooks_KXBTCD.json",
                "orderbooks_KXNEXTDNCCHAIR-45.json",
            )
            for entry in raw(name)["orderbooks"]
        }
        self.markets = [market for event in self.events for market in event.get("markets", [])]
        self.orderbook_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.split("/trade-api/v2", 1)[1]
        if endpoint == "/exchange/status":
            return self._json(raw("exchange_status.json"))
        if endpoint == "/series":
            return self._json(raw("series_subset.json"))
        if endpoint == "/events":
            return self._json({"events": self.events, "cursor": ""})
        if endpoint == "/markets":
            return self._json({"markets": self.markets, "cursor": ""})
        if endpoint == "/markets/orderbooks":
            self.orderbook_requests += 1
            wanted = request.url.params.get_list("tickers")
            assert len(wanted) <= 100
            return self._json({"orderbooks": [self.books[t] for t in wanted if t in self.books]})
        return httpx.Response(404, content=b"not recorded")

    @staticmethod
    def _json(payload: object) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())


def make_scanner(exchange: RecordedExchange, config: ScanConfig) -> tuple[KalshiClient, Scanner]:
    clock = FakeClock(captured_at())

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)

    client = KalshiClient(
        base_url=BASE,
        transport=httpx.MockTransport(exchange),
        clock=clock,
        sleep=sleep,
        rng=random.Random(1),
    )
    return client, Scanner(client, config, sleep=sleep)


async def test_run_once_end_to_end_on_recorded_data() -> None:
    exchange = RecordedExchange()
    client, scanner = make_scanner(exchange, ScanConfig(watchlist_size=10))
    async with client:
        report = await scanner.run_once()

    universe = scanner.universe
    assert universe is not None
    assert universe.events_seen == 4
    assert set(universe.structures) == ELIGIBLE
    assert universe.exclusions == {Exclusion.SINGLE_MARKET: 1}
    assert set(report.targets) == ELIGIBLE
    assert set(report.detections) == ELIGIBLE
    assert report.fetch_errors == [] and report.integrity == []
    # Two confirmation passes; the 188-market ladder needs two requests per pass on its own.
    assert 4 <= exchange.orderbook_requests <= 8
    # Recorded books admit no arbitrage after fees: the normal, correct result.
    assert scanner.tracker.live() == []


async def test_listing_refresh_updates_quotes_without_reclassifying() -> None:
    exchange = RecordedExchange()
    client, scanner = make_scanner(exchange, ScanConfig())
    async with client:
        universe = await scanner.discover()
        structure_before = universe.structures["KXINX-26SEP14H1600"]
        target = structure_before.event.markets[0].ticker
        exchange.markets = [
            {**market, "yes_bid_dollars": "0.0050", "yes_bid_size_fp": "7.00"}
            if market["ticker"] == target
            else market
            for market in exchange.markets
        ]
        await scanner.refresh_listings()

    structure_after = universe.structures["KXINX-26SEP14H1600"]
    refreshed = structure_after.event.market(target)
    assert str(refreshed.quote.yes_bid) == "0.0050"
    assert structure_after.spaces is structure_before.spaces
