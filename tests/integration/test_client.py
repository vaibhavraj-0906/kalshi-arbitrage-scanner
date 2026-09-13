"""The client against simulated failure modes Kalshi actually exhibits from this network."""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from karb.core.clock import FakeClock
from karb.exchange.client import (
    KalshiClient,
    KalshiHTTPError,
    KalshiUnavailable,
    RetryPolicy,
    TokenBucket,
)
from karb.exchange.endpoints import (
    SkipLog,
    fetch_orderbooks,
    iter_events,
    pack_batches,
    trading_shards,
)
from karb.wire.models import ExchangeStatusWire
from tests.support import NOW, load_fixture

BASE = "https://kalshi.test/trade-api/v2"


class SleepRecorder:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], *, max_attempts: int = 5
) -> tuple[KalshiClient, SleepRecorder]:
    clock = FakeClock(NOW)
    sleeper = SleepRecorder(clock)
    client = KalshiClient(
        base_url=BASE,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=sleeper,
        rng=random.Random(7),
        rate=1_000.0,
        burst=1_000.0,
        retry=RetryPolicy(max_attempts=max_attempts),
    )
    return client, sleeper


def ok(payload: object) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode())


async def test_connection_resets_are_retried_and_numbers_decode_exactly() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, content=b'{"floor_strike": 7249.9999}')

    client, sleeper = make_client(handler)
    async with client:
        payload = await client.get("/anything")
    assert payload == {"floor_strike": Decimal("7249.9999")}
    assert (client.stats.resets, client.stats.retries, len(sleeper.sleeps)) == (2, 2, 2)


async def test_throttling_server_errors_and_truncated_bodies_are_retried() -> None:
    responses = [
        httpx.Response(429, content=b'{"error": "too many requests"}'),
        httpx.Response(503, content=b"unavailable"),
        httpx.Response(200, content=b'{"events": [{"event_tic'),
        ok({"fine": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client, _ = make_client(handler)
    async with client:
        assert await client.get("/x") == {"fine": True}
    assert (client.stats.throttled, client.stats.server_errors, client.stats.resets) == (1, 1, 1)


async def test_client_errors_are_not_retried() -> None:
    client, sleeper = make_client(lambda request: httpx.Response(404, content=b"no such market"))
    async with client:
        with pytest.raises(KalshiHTTPError) as caught:
            await client.get("/markets/NOPE")
    assert caught.value.status == 404
    assert sleeper.sleeps == []


async def test_gives_up_after_the_retry_budget() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, sleeper = make_client(handler, max_attempts=4)
    async with client:
        with pytest.raises(KalshiUnavailable, match="failed 4 times"):
            await client.get("/x")
    assert client.stats.failures == 1
    assert len(sleeper.sleeps) == 3
    assert all(0 <= s <= 8.0 for s in sleeper.sleeps)


async def test_pagination_follows_cursors_and_skips_bad_items() -> None:
    pages = {
        None: {"events": [{"event_ticker": "E1"}, {"no_ticker": True}], "cursor": "c2"},
        "c2": {"events": [{"event_ticker": "E2"}], "cursor": ""},
    }
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return ok(pages[request.url.params.get("cursor")])

    client, _ = make_client(handler)
    skips = SkipLog()
    async with client:
        events = [e.event_ticker async for e in iter_events(client, skips=skips)]
    assert events == ["E1", "E2"]
    assert skips.counts == {"event": 1}
    assert seen_params[0] == {"status": "open", "with_nested_markets": "true", "limit": "200"}
    assert seen_params[1]["cursor"] == "c2"


async def test_orderbooks_request_repeats_the_tickers_parameter() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return ok(load_fixture("orderbooks_KXNEXTDNCCHAIR-45.json"))

    client, _ = make_client(handler)
    async with client:
        batch = await fetch_orderbooks(client, ["A", "B", "C"], depth=10)
    assert captured[0].url.params.get_list("tickers") == ["A", "B", "C"]
    assert captured[0].url.params["depth"] == "10"
    assert len(batch.books) == 34 and batch.problems == ()
    with pytest.raises(ValueError):
        await fetch_orderbooks(client, [f"T{i}" for i in range(101)])


async def test_token_bucket_paces_requests() -> None:
    clock = FakeClock(NOW)
    sleeper = SleepRecorder(clock)
    bucket = TokenBucket(rate=2.0, capacity=1.0, clock=clock, sleep=sleeper)
    for _ in range(3):
        await bucket.acquire()
    assert sum(sleeper.sleeps) == pytest.approx(1.0)


def test_pack_batches_keeps_events_whole() -> None:
    groups = [["a"] * 60, ["b"] * 30, ["c"] * 20, ["d"] * 250]
    batches = pack_batches(groups)
    assert [len(b) for b in batches] == [90, 20, 100, 100, 50]
    assert batches[0] == ["a"] * 60 + ["b"] * 30


def test_trading_shards() -> None:
    status = ExchangeStatusWire.model_validate(load_fixture("exchange_status.json"))
    assert trading_shards(status) == {0, 1, 2, 3}
    halted = status.model_copy(update={"trading_active": False})
    assert trading_shards(halted) == frozenset()
