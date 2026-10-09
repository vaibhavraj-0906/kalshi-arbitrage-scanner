"""A mock Kalshi exchange: the recorded S&P range event plus a planted three-way overround.

The planted event's YES bids sum to $1.20, so buying NO on all three locks in $7.48 on 50
contracts each (see tests/unit/test_detect.py for the arithmetic). Paper-trading tests can thin
chosen markets on "arrival" -- requests for nothing but planted markets, which only the paper
trader makes -- and serve settlement results.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from karb.core.clock import FakeClock
from karb.exchange.client import KalshiClient
from karb.scanner.service import CycleReport, ScanConfig, Scanner
from karb.store.database import RecordStore
from karb.store.recorder import Recorder
from tests.support import FIXTURES, captured_at

BASE = "https://kalshi.test/trade-api/v2"
CLOSE = "2026-10-01T00:00:00Z"
PLANTED = ("PLANT-1-A", "PLANT-1-B", "PLANT-1-C")


def raw(name: str) -> Any:
    return json.loads((FIXTURES / name).read_bytes())


def planted_market(ticker: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": "PLANT-1",
        "market_type": "binary",
        "status": "active",
        "title": ticker,
        "yes_sub_title": ticker,
        "strike_type": "custom",
        "yes_bid_dollars": "0.4000",
        "yes_ask_dollars": "0.4500",
        "yes_bid_size_fp": "50.00",
        "yes_ask_size_fp": "50.00",
        "volume_24h_fp": "1000.00",
        "close_time": CLOSE,
        "latest_expiration_time": CLOSE,
        "exchange_index": 0,
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }


class PlantedExchange:
    def __init__(
        self,
        *,
        thin_on_arrival: frozenset[str] = frozenset(),
        results: dict[str, tuple[str, str]] | None = None,
        failing_event_calls: frozenset[int] = frozenset(),
    ) -> None:
        self.events = [
            {
                "event_ticker": "PLANT-1",
                "series_ticker": "PLANT",
                "title": "Planted overround",
                "mutually_exclusive": True,
                "collateral_return_type": "MECNET",
                "markets": [planted_market(ticker) for ticker in PLANTED],
            },
            raw("events_KXINX.json")["events"][0],
        ]
        self.series = [
            *raw("series_subset.json")["series"],
            {"ticker": "PLANT", "fee_type": "quadratic", "fee_multiplier": 1},
        ]
        self.books = {
            entry["ticker"]: entry for entry in raw("orderbooks_KXINX.json")["orderbooks"]
        }
        for ticker in PLANTED:
            self.books[ticker] = {
                "ticker": ticker,
                "orderbook_fp": {
                    "yes_dollars": [["0.4000", "50.00"]],
                    "no_dollars": [["0.5500", "50.00"]],
                },
            }
        self.thin_on_arrival = thin_on_arrival
        self.results = results or {}
        """ticker -> (status, result) served by GET /markets?tickers=..."""
        self.arrival_fetches = 0
        self.failing_event_calls = failing_event_calls
        """1-based numbers of GET /events calls that fail as if DNS were down."""
        self.event_calls = 0

    def _book(self, ticker: str, *, arriving: bool) -> dict[str, Any]:
        book = self.books[ticker]
        if arriving and ticker in self.thin_on_arrival:
            # Someone took every YES bid, so NO can no longer be bought here.
            return {
                "ticker": ticker,
                "orderbook_fp": {**book["orderbook_fp"], "yes_dollars": []},
            }
        return book

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.split("/trade-api/v2", 1)[1]
        payload: object
        if endpoint == "/exchange/status":
            payload = raw("exchange_status.json")
        elif endpoint == "/series":
            payload = {"series": self.series}
        elif endpoint == "/events":
            self.event_calls += 1
            if self.event_calls in self.failing_event_calls:
                raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)
            payload = {"events": self.events, "cursor": ""}
        elif endpoint == "/markets/orderbooks":
            wanted = request.url.params.get_list("tickers")
            arriving = set(wanted) <= set(PLANTED)
            self.arrival_fetches += int(arriving)
            payload = {
                "orderbooks": [self._book(t, arriving=arriving) for t in wanted if t in self.books]
            }
        elif endpoint == "/markets":
            tickers = request.url.params.get("tickers", "").split(",")
            payload = {
                "markets": [
                    {
                        "ticker": t,
                        "event_ticker": "PLANT-1",
                        "status": status,
                        "result": result,
                        "settlement_value_dollars": {"yes": "1.0000", "no": "0.0000"}.get(result),
                        "settlement_ts": "2026-10-01T00:05:00Z",
                    }
                    for t in tickers
                    if t in self.results
                    for status, result in [self.results[t]]
                ],
                "cursor": "",
            }
        else:
            return httpx.Response(404, content=b"not recorded")
        return httpx.Response(200, content=json.dumps(payload).encode())


def make_client(exchange: PlantedExchange) -> tuple[KalshiClient, Callable[[float], Any]]:
    clock = FakeClock(captured_at())

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)  # yield, so background tasks interleave as they would live

    client = KalshiClient(
        base_url=BASE,
        transport=httpx.MockTransport(exchange),
        clock=clock,
        sleep=sleep,
        rng=random.Random(1),
    )
    return client, sleep


async def record_scan(
    path: Path,
    *,
    exchange: PlantedExchange | None = None,
    confirmations: int = 2,
    on_cycle: Callable[[Scanner, CycleReport, int], None] | None = None,
) -> str:
    client, sleep = make_client(exchange or PlantedExchange())
    with RecordStore(path) as store:
        recorder = Recorder(store, client.clock)
        async with client:
            scanner = Scanner(
                client,
                ScanConfig(watchlist_size=10, confirmations=confirmations),
                sleep=sleep,
                record_payloads=True,
            )
            run_id = recorder.start_run(scanner.config)

            def handle(report: CycleReport) -> None:
                cycle_no = recorder.record_cycle(
                    report, payloads=scanner.event_payloads, series=scanner.series
                )
                if on_cycle is not None:
                    on_cycle(scanner, report, cycle_no)

            await scanner.run_once(on_cycle=handle)
        recorder.finish_run()
    return run_id
