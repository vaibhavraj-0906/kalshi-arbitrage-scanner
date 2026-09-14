"""Recording a scan, replaying it, and measuring it -- end to end, offline.

A mock exchange serves the recorded S&P range event alongside a planted three-way event whose YES
bids sum to $1.20, so the recording holds a real (planted) opportunity to reproduce.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import httpx

from karb.core.clock import FakeClock
from karb.exchange.client import KalshiClient
from karb.scanner.service import ScanConfig, Scanner
from karb.store.codec import detect_config_from_json
from karb.store.database import LIVE_SOURCE, RecordStore
from karb.store.recorder import Recorder
from karb.store.replay import compare_with_live, replay_run
from karb.store.stats import fee_scenarios, fee_sensitivity, run_statistics
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
    def __init__(self) -> None:
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

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.split("/trade-api/v2", 1)[1]
        payload: object
        if endpoint == "/exchange/status":
            payload = raw("exchange_status.json")
        elif endpoint == "/series":
            payload = {"series": self.series}
        elif endpoint == "/events":
            payload = {"events": self.events, "cursor": ""}
        elif endpoint == "/markets/orderbooks":
            wanted = request.url.params.get_list("tickers")
            payload = {"orderbooks": [self.books[t] for t in wanted if t in self.books]}
        else:
            return httpx.Response(404, content=b"not recorded")
        return httpx.Response(200, content=json.dumps(payload).encode())


async def record_scan(path: Path) -> str:
    clock = FakeClock(captured_at())

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)

    client = KalshiClient(
        base_url=BASE,
        transport=httpx.MockTransport(PlantedExchange()),
        clock=clock,
        sleep=sleep,
        rng=random.Random(1),
    )
    with RecordStore(path) as store:
        recorder = Recorder(store, clock)
        async with client:
            scanner = Scanner(
                client, ScanConfig(watchlist_size=10), sleep=sleep, record_payloads=True
            )
            run_id = recorder.start_run(scanner.config)
            await scanner.run_once(
                on_cycle=lambda report: recorder.record_cycle(
                    report, payloads=scanner.event_payloads, series=scanner.series
                )
            )
        recorder.finish_run()
    return run_id


async def test_recording_captures_every_cycle(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await record_scan(path)
    with RecordStore(path, read_only=True) as store:
        (run,) = store.runs()
        assert (run.run_id, run.cycles, run.observations, run.live_opportunities) == (
            run_id,
            2,
            4,
            2,
        )
        assert run.finished_ns is not None
        counts = store.table_counts()
        assert counts["event_payloads"] == 2  # stored once each, referenced by both cycles
        assert counts["books"] == 2 * (len(PLANTED) + 30)
        live = store.opportunities(LIVE_SOURCE, run_id)
        assert len({row.opportunity_id for row in live}) == 1
        assert {row.guaranteed_pnl for row in live} == {7_480_000}


async def test_replay_reproduces_the_live_run_exactly(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await record_scan(path)
    with RecordStore(path) as store:
        recorded = detect_config_from_json(store.run(run_id).config["detect"])
        outcome = replay_run(store, run_id, recorded)
        assert (outcome.observations, outcome.replayed, dict(outcome.skipped)) == (4, 4, {})
        assert compare_with_live(store, outcome).identical

        saved = replay_run(store, run_id, recorded, save=True, now=captured_at())
        assert saved.replay_id is not None
        assert len(store.opportunities(saved.replay_id, run_id)) == 2


async def test_statistics_and_fee_sensitivity(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await record_scan(path)
    with RecordStore(path, read_only=True) as store:
        stats = run_statistics(store, run_id)
        assert (stats.cycles, stats.rescreens, stats.observations, stats.groups) == (2, 1, 4, 2)
        assert stats.verified_observations == 2
        (episode,) = stats.episodes
        assert (episode.kind, episode.sightings, episode.censored) == ("OVERROUND", 2, True)
        assert episode.lifetime_seconds == 5.0
        rules = {(tier, rule) for tier, rule, _hits, _groups in stats.screen_hits}
        assert ("LOGICAL", "bids over $1") in rules

        recorded = detect_config_from_json(store.run(run_id).config["detect"])
        rows = fee_sensitivity(store, run_id, recorded)
        assert [row.label for row in rows] == [label for label, _ in fee_scenarios(recorded)]
        as_recorded, free = rows[0], rows[1]
        assert (as_recorded.verified_observations, as_recorded.episodes) == (2, 1)
        assert as_recorded.best_total == 7_480_000
        assert free.best_total == 10_000_000  # $100 guaranteed for $90 of NO, no fees at all
