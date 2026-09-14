"""The public Kalshi endpoints the scanner reads, returned as domain objects.

Pages are decoded one item at a time: a single malformed market must cost the scanner that
market, not a 1,000-market sweep. Every skip is counted, with an example, in a ``SkipLog``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import ValidationError

from karb.core.fixed import FixedPointError
from karb.exchange.client import KalshiClient, KalshiHTTPError
from karb.market.book import BookIntegrityError, OrderBook
from karb.market.model import (
    EventInfo,
    MarketInfo,
    SeriesInfo,
    event_from_wire,
    market_from_wire,
    series_from_wire,
)
from karb.wire.models import (
    EventResponseWire,
    EventWire,
    ExchangeStatusWire,
    MarketWire,
    OrderbooksWire,
    SeriesWire,
)

__all__ = [
    "EVENTS_PAGE_LIMIT",
    "MARKETS_PAGE_LIMIT",
    "ORDERBOOKS_PER_REQUEST",
    "BookBatch",
    "SkipLog",
    "fetch_event",
    "fetch_exchange_status",
    "fetch_one_series",
    "fetch_orderbooks",
    "fetch_series",
    "iter_event_payloads",
    "iter_events",
    "iter_market_listings",
    "pack_batches",
    "trading_shards",
]

EVENTS_PAGE_LIMIT: Final = 200
MARKETS_PAGE_LIMIT: Final = 1000
ORDERBOOKS_PER_REQUEST: Final = 100


@dataclass
class SkipLog:
    counts: Counter[str] = field(default_factory=Counter)
    examples: dict[str, str] = field(default_factory=dict)

    def record(self, kind: str, detail: str) -> None:
        self.counts[kind] += 1
        self.examples.setdefault(kind, detail[:200])

    @property
    def total(self) -> int:
        return sum(self.counts.values())


async def fetch_exchange_status(client: KalshiClient) -> ExchangeStatusWire:
    return ExchangeStatusWire.model_validate(await client.get("/exchange/status"))


def trading_shards(status: ExchangeStatusWire) -> frozenset[int] | None:
    """Exchange shards currently accepting trades; ``None`` if no per-shard detail was given."""
    if not (status.exchange_active and status.trading_active):
        return frozenset()
    if not status.exchange_index_statuses:
        return None
    return frozenset(
        shard.exchange_index
        for shard in status.exchange_index_statuses
        if shard.exchange_active and shard.trading_active
    )


async def fetch_series(client: KalshiClient, skips: SkipLog | None = None) -> dict[str, SeriesInfo]:
    """Every series, with fee metadata, in one request (``GET /series`` is unpaginated)."""
    payload = await client.get("/series")
    series: dict[str, SeriesInfo] = {}
    for raw in payload.get("series") or []:
        try:
            wire = SeriesWire.model_validate(raw)
        except ValidationError as exc:
            if skips is not None:
                skips.record("series", str(exc))
            continue
        series[wire.ticker] = series_from_wire(wire)
    return series


async def fetch_one_series(client: KalshiClient, series_ticker: str) -> SeriesInfo | None:
    """One series' fee metadata; ``None`` if Kalshi does not know the ticker."""
    try:
        payload = await client.get(f"/series/{series_ticker}")
    except KalshiHTTPError as exc:
        if exc.status == 404:
            return None
        raise
    return series_from_wire(SeriesWire.model_validate(payload["series"]))


async def iter_event_payloads(
    client: KalshiClient,
    *,
    status: str = "open",
    series_ticker: str | None = None,
    max_pages: int | None = None,
    skips: SkipLog | None = None,
) -> AsyncIterator[tuple[EventInfo, dict[str, Any]]]:
    """Events with their markets nested, each with the raw payload it was decoded from.

    Recording keeps the payload rather than the decoded event, because classification evolves
    and a replay must be able to re-read fields today's model ignores (ADR-0007).
    """
    params: list[tuple[str, str | int]] = [
        ("status", status),
        ("with_nested_markets", "true"),
        ("limit", EVENTS_PAGE_LIMIT),
    ]
    if series_ticker is not None:
        params.append(("series_ticker", series_ticker))
    async for page in client.paginate("/events", params, "events", max_pages=max_pages):
        for raw in page:
            try:
                event = event_from_wire(EventWire.model_validate(raw))
            except (ValidationError, FixedPointError) as exc:
                if skips is not None:
                    skips.record("event", f"{_ticker(raw, 'event_ticker')}: {exc}")
                continue
            yield event, raw


async def iter_events(
    client: KalshiClient,
    *,
    status: str = "open",
    series_ticker: str | None = None,
    max_pages: int | None = None,
    skips: SkipLog | None = None,
) -> AsyncIterator[EventInfo]:
    """Events with their markets nested. ``/events`` excludes multivariate events."""
    async for event, _payload in iter_event_payloads(
        client, status=status, series_ticker=series_ticker, max_pages=max_pages, skips=skips
    ):
        yield event


async def fetch_event(client: KalshiClient, event_ticker: str) -> EventInfo:
    payload = await client.get(f"/events/{event_ticker}", [("with_nested_markets", "true")])
    response = EventResponseWire.model_validate(payload)
    return event_from_wire(response.event, response.markets)


async def iter_market_listings(
    client: KalshiClient,
    *,
    status: str = "open",
    series_ticker: str | None = None,
    max_pages: int | None = None,
    skips: SkipLog | None = None,
) -> AsyncIterator[MarketInfo]:
    """Market listings with top-of-book quotes, multivariate combos excluded."""
    params: list[tuple[str, str | int]] = [
        ("status", status),
        ("mve_filter", "exclude"),
        ("limit", MARKETS_PAGE_LIMIT),
    ]
    if series_ticker is not None:
        params.append(("series_ticker", series_ticker))
    async for page in client.paginate("/markets", params, "markets", max_pages=max_pages):
        for raw in page:
            try:
                yield market_from_wire(MarketWire.model_validate(raw))
            except (ValidationError, FixedPointError) as exc:
                if skips is not None:
                    skips.record("market", f"{_ticker(raw, 'ticker')}: {exc}")


@dataclass(frozen=True, slots=True)
class BookBatch:
    books: dict[str, OrderBook]
    requested_at: float
    """Monotonic time the request was sent."""
    received_at: float
    """Monotonic time the response arrived."""
    problems: tuple[str, ...]


async def fetch_orderbooks(
    client: KalshiClient, tickers: Sequence[str], *, depth: int = 0
) -> BookBatch:
    """Books for up to 100 markets in one request: one near-simultaneous snapshot."""
    if not 0 < len(tickers) <= ORDERBOOKS_PER_REQUEST:
        raise ValueError(f"between 1 and {ORDERBOOKS_PER_REQUEST} tickers per request")
    params: list[tuple[str, str | int]] = [("tickers", ticker) for ticker in tickers]
    if depth > 0:
        params.append(("depth", depth))
    requested_at = client.clock.monotonic()
    payload = await client.get("/markets/orderbooks", params)
    received_at = client.clock.monotonic()
    books: dict[str, OrderBook] = {}
    problems: list[str] = []
    for entry in OrderbooksWire.model_validate(payload).orderbooks:
        try:
            books[entry.ticker] = OrderBook.from_wire(
                entry.ticker, entry.orderbook_fp.yes_dollars, entry.orderbook_fp.no_dollars
            )
        except BookIntegrityError as exc:
            problems.append(str(exc))
    return BookBatch(books, requested_at, received_at, tuple(problems))


def pack_batches(
    groups: Sequence[Sequence[str]], limit: int = ORDERBOOKS_PER_REQUEST
) -> list[list[str]]:
    """Pack each group's tickers into requests of at most ``limit``.

    A group (an event) that fits is never split, so its books arrive in one response. A group
    larger than ``limit`` gets consecutive requests of its own.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    for group in groups:
        if len(group) > limit:
            if current:
                batches.append(current)
                current = []
            batches.extend(list(group[i : i + limit]) for i in range(0, len(group), limit))
            continue
        if len(current) + len(group) > limit:
            batches.append(current)
            current = []
        current.extend(group)
    if current:
        batches.append(current)
    return batches


def _ticker(raw: object, key: str) -> str:
    return str(raw.get(key, "?")) if isinstance(raw, dict) else "?"
