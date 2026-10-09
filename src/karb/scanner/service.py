"""The scanner: three polling tiers over the public Kalshi API (docs/decisions/ADR-0004).

Tier A -- discovery (minutes)
    ``GET /series`` for fee metadata and ``GET /events`` for every open event with its markets,
    then classify each event once. Structure changes rarely, so classification is cached.
Tier B -- screen (a minute or two)
    Refresh market listings (top-of-book quotes) and run the integer screens over every
    eligible event. Hits become candidates; 24-hour volume ranks a standing watchlist.
Tier C -- confirm (seconds)
    Fetch whole events' books through ``GET /markets/orderbooks`` -- up to 100 markets per
    request, so an event usually arrives as a single snapshot -- then solve, verify, and track
    what persists across snapshots.

Kalshi's WebSockets would make Tier C push-based, but they require authentication, and this
project deliberately runs on public data only.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from karb.arb.detect import DetectConfig, Detection, EventSnapshot, detect
from karb.arb.opportunity import OpportunityTracker
from karb.arb.screen import ScreenHit, screen_event
from karb.exchange.client import KalshiClient, KalshiError
from karb.exchange.endpoints import (
    BookBatch,
    SkipLog,
    fetch_exchange_status,
    fetch_orderbooks,
    fetch_series,
    iter_event_payloads,
    iter_market_listings,
    pack_batches,
    trading_shards,
)
from karb.market.book import OrderBook
from karb.market.model import EventInfo, MarketInfo, SeriesInfo
from karb.store.codec import dumps_exact, structural_payload
from karb.structure.classify import (
    DEFAULT_TRADEABILITY,
    EventStructure,
    Exclusion,
    TradeabilityRules,
    classify_event,
    split_by_participant,
    tradeable_tickers,
)

__all__ = ["CycleReport", "ScanConfig", "Scanner", "Universe"]

Progress = Callable[[str], object]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ScanConfig:
    detect: DetectConfig = field(default_factory=DetectConfig)
    tradeability: TradeabilityRules = DEFAULT_TRADEABILITY
    series: tuple[str, ...] = ()
    """Restrict discovery to these series tickers. Empty means every open event."""
    max_event_pages: int | None = None
    """Cap discovery at this many pages of 200 events (per series when ``series`` is set)."""
    asserted_exhaustive: frozenset[str] = frozenset()
    watchlist_size: int = 40
    confirm_all: bool = False
    """Confirm every eligible event each cycle, not just screen hits and the watchlist."""
    confirmations: int = 2
    discovery_interval: float = 600.0
    screen_interval: float = 90.0
    confirm_interval: float = 5.0
    fetch_concurrency: int = 4


@dataclass
class Universe:
    discovered_at: datetime
    events_seen: int
    groups_seen: int
    """Events after splitting multi-participant strike events into per-participant groups."""
    structures: dict[str, EventStructure]
    exclusions: Counter[Exclusion]
    exclusion_examples: dict[Exclusion, str]
    series_count: int
    shards: frozenset[int] | None

    def refresh_listings(self, listings: dict[str, MarketInfo]) -> int:
        """Swap in newer market listings, keeping structure. Returns groups touched."""
        touched = 0
        for key, structure in self.structures.items():
            if any(market.ticker in listings for market in structure.event.markets):
                refreshed = structure.event.with_listings(listings)
                self.structures[key] = replace(structure, event=refreshed)
                touched += 1
        return touched


@dataclass
class CycleReport:
    started_at: datetime
    finished_at: datetime
    rescreened: bool
    """Whether this cycle re-ran the screens; otherwise ``hits`` are an earlier cycle's."""
    screened: int
    """Eligible events with at least two tradeable markets."""
    untradeable: int
    hits: dict[str, list[ScreenHit]]
    targets: list[str]
    detections: dict[str, Detection]
    snapshots: dict[str, EventSnapshot] = field(default_factory=dict)
    """The exact inputs behind each detection: what a recorder writes and a replay rebuilds."""
    fetch_errors: list[str] = field(default_factory=list)

    @property
    def integrity(self) -> list[str]:
        return [issue for detection in self.detections.values() for issue in detection.integrity]


def _liquidity(structure: EventStructure) -> int:
    return sum(market.volume_24h.raw for market in structure.event.markets)


class Scanner:
    def __init__(
        self,
        client: KalshiClient,
        config: ScanConfig | None = None,
        *,
        progress: Progress | None = None,
        sleep: Sleep | None = None,
        record_payloads: bool = False,
    ) -> None:
        self.client = client
        self.config = config or ScanConfig()
        self.progress: Progress = progress or (lambda _message: None)
        self._sleep: Sleep = sleep or asyncio.sleep
        self.record_payloads = record_payloads
        self.tracker = OpportunityTracker(self.config.confirmations)
        self.skips = SkipLog()
        self.universe: Universe | None = None
        self.series: dict[str, SeriesInfo] = {}
        self.event_payloads: dict[str, str] = {}
        """Structural JSON per event ticker, kept only with ``record_payloads`` (ADR-0007)."""
        self._hits: dict[str, list[ScreenHit]] = {}
        self._targets: list[str] = []
        self._screened = 0
        self._untradeable = 0
        self.outages: list[str] = []
        """Background discoveries and listing refreshes that failed, newest last."""

    def _require_universe(self) -> Universe:
        if self.universe is None:
            raise RuntimeError("discover() must run before screening or confirming")
        return self.universe

    def _tradeable(self, structure: EventStructure, now: datetime) -> frozenset[str]:
        shards = self.universe.shards if self.universe is not None else None
        return tradeable_tickers(
            structure.event, now=now, trading_shards=shards, rules=self.config.tradeability
        )

    # --- Tier A ------------------------------------------------------------------------------

    async def discover(self) -> Universe:
        config = self.config
        self.progress("Checking exchange status")
        shards = trading_shards(await fetch_exchange_status(self.client))
        self.progress("Loading series fee schedules")
        series = await fetch_series(self.client, self.skips)

        events: dict[str, EventInfo] = {}
        payloads: dict[str, str] = {}
        scopes: Sequence[str | None] = config.series or (None,)
        for scope in scopes:
            async for event, raw in iter_event_payloads(
                self.client, series_ticker=scope, max_pages=config.max_event_pages, skips=self.skips
            ):
                events[event.event_ticker] = event
                if self.record_payloads:
                    # Serialised immediately: thousands of events are far lighter as strings.
                    payloads[event.event_ticker] = dumps_exact(structural_payload(raw))
                if len(events) % 500 == 0:
                    self.progress(f"Discovered {len(events):,} open events")

        groups = [group for event in events.values() for group in split_by_participant(event)]
        structures: dict[str, EventStructure] = {}
        exclusions: Counter[Exclusion] = Counter()
        examples: dict[Exclusion, str] = {}
        for group in groups:
            result = classify_event(
                group,
                series.get(group.series_ticker),
                asserted_exhaustive=config.asserted_exhaustive,
            )
            if result.structure is not None:
                structures[group.event_ticker] = result.structure
            elif result.exclusion is not None:
                exclusions[result.exclusion] += 1
                examples.setdefault(
                    result.exclusion, f"{group.event_ticker} {result.detail}".strip()
                )

        # Merged, not replaced: a cycle already in flight when this discovery lands may still
        # record a group whose event has just closed, and the recorder needs its payload.
        self.series.update(series)
        self.event_payloads.update(payloads)
        self.universe = Universe(
            discovered_at=self.client.clock.now(),
            events_seen=len(events),
            groups_seen=len(groups),
            structures=structures,
            exclusions=exclusions,
            exclusion_examples=examples,
            series_count=len(series),
            shards=shards,
        )
        self._targets = []
        self.progress(f"Classified {len(groups):,} event groups: {len(structures):,} scannable")
        return self.universe

    # --- Tier B ------------------------------------------------------------------------------

    async def refresh_listings(self) -> None:
        universe = self._require_universe()
        listings: dict[str, MarketInfo] = {}
        for scope in self.config.series or (None,):
            async for market in iter_market_listings(
                self.client, series_ticker=scope, skips=self.skips
            ):
                listings[market.ticker] = market
        universe.refresh_listings(listings)
        self.progress(f"Refreshed {len(listings):,} market listings")

    def screen(self, now: datetime) -> tuple[dict[str, list[ScreenHit]], list[str], int, int]:
        """Screen hits, confirmation targets, groups screened, groups untradeable."""
        universe = self._require_universe()
        hits: dict[str, list[ScreenHit]] = {}
        eligible: list[EventStructure] = []
        untradeable = 0
        for key, structure in universe.structures.items():
            tradeable = self._tradeable(structure, now)
            if len(tradeable) < 2:
                untradeable += 1
                continue
            eligible.append(structure)
            quotes = {market.ticker: market.quote for market in structure.event.markets}
            found = screen_event(structure, quotes, tradeable)
            if found:
                hits[key] = found
        if self.config.confirm_all:
            targets = [structure.event.event_ticker for structure in eligible]
        else:
            ranked = sorted(eligible, key=_liquidity, reverse=True)[: self.config.watchlist_size]
            targets = list(dict.fromkeys([*hits, *(s.event.event_ticker for s in ranked)]))
        return hits, targets, len(eligible), untradeable

    # --- Tier C ------------------------------------------------------------------------------

    async def confirm(
        self, targets: Sequence[str]
    ) -> tuple[dict[str, Detection], dict[str, EventSnapshot], list[str]]:
        """Detections and the snapshots behind them, by group, plus any fetch errors."""
        universe = self._require_universe()
        now = self.client.clock.now()
        plans: list[tuple[EventStructure, frozenset[str]]] = []
        for key in targets:
            structure = universe.structures.get(key)
            if structure is None:
                continue
            tradeable = self._tradeable(structure, now)
            if len(tradeable) >= 2:
                plans.append((structure, tradeable))

        books, timing, errors = await self._fetch_books(
            pack_batches([sorted(tradeable) for _, tradeable in plans])
        )
        observed_at = self.client.clock.now()
        detections: dict[str, Detection] = {}
        snapshots: dict[str, EventSnapshot] = {}
        for structure, tradeable in plans:
            spans = [timing[ticker] for ticker in tradeable if ticker in timing]
            if not spans:
                continue
            snapshot = EventSnapshot(
                structure=structure,
                books={ticker: books[ticker] for ticker in tradeable if ticker in books},
                tradeable=tradeable,
                observed_at=observed_at,
                skew_seconds=max(received for _, received in spans)
                - min(sent for sent, _ in spans),
            )
            detection = detect(snapshot, self.config.detect)
            key = structure.event.event_ticker
            self.tracker.observe(key, detection.opportunities, observed_at)
            detections[key] = detection
            snapshots[key] = snapshot
        return detections, snapshots, errors

    async def _fetch_books(
        self, batches: list[list[str]]
    ) -> tuple[dict[str, OrderBook], dict[str, tuple[float, float]], list[str]]:
        semaphore = asyncio.Semaphore(self.config.fetch_concurrency)

        async def fetch(batch: list[str]) -> tuple[list[str], BookBatch | None, str]:
            async with semaphore:
                try:
                    fetched = await fetch_orderbooks(
                        self.client, batch, depth=self.config.detect.max_levels
                    )
                except KalshiError as exc:
                    return batch, None, str(exc)
                return batch, fetched, ""

        books: dict[str, OrderBook] = {}
        timing: dict[str, tuple[float, float]] = {}
        errors: list[str] = []
        for batch, fetched, error in await asyncio.gather(*(fetch(b) for b in batches)):
            if fetched is None:
                errors.append(error)
                continue
            books.update(fetched.books)
            errors.extend(fetched.problems)
            for ticker in batch:
                timing[ticker] = (fetched.requested_at, fetched.received_at)
        return books, timing, errors

    # --- loops -------------------------------------------------------------------------------

    async def cycle(self, *, rescreen: bool) -> CycleReport:
        started = self.client.clock.now()
        rescreened = rescreen or not self._targets
        if rescreened:
            self._hits, self._targets, self._screened, self._untradeable = self.screen(started)
        self.progress(f"Confirming books for {len(self._targets):,} event groups")
        detections, snapshots, errors = await self.confirm(self._targets)
        return CycleReport(
            started_at=started,
            finished_at=self.client.clock.now(),
            rescreened=rescreened,
            screened=self._screened,
            untradeable=self._untradeable,
            hits=self._hits,
            targets=list(self._targets),
            detections=detections,
            snapshots=snapshots,
            fetch_errors=errors,
        )

    async def run_once(
        self, on_cycle: Callable[[CycleReport], object] | None = None
    ) -> CycleReport:
        """Discover, screen, then confirm ``confirmations`` times in a row.

        ``on_cycle`` sees every cycle's report, not only the last: a recorder needs them all.
        """
        await self.discover()
        report = await self.cycle(rescreen=True)
        if on_cycle is not None:
            on_cycle(report)
        for _ in range(self.config.confirmations - 1):
            await self._sleep(self.config.confirm_interval)
            report = await self.cycle(rescreen=False)
            if on_cycle is not None:
                on_cycle(report)
        return report

    async def run_forever(
        self, on_cycle: Callable[[CycleReport], object], *, stop_after: float | None = None
    ) -> None:
        """Scan until cancelled, or until ``stop_after`` seconds have passed.

        Confirmation (Tier C) runs continuously. Discovery and listing refreshes (Tiers A and B)
        take minutes over a lossy link, so they run in the background and are applied when they
        finish; confirmation keeps going against the previous universe meanwhile. A failed
        background refresh is recorded as an outage and retried with backoff -- a network blip
        must not end a long recording.
        """
        config, clock = self.config, self.client.clock
        started = clock.monotonic()

        def stopping() -> bool:
            return stop_after is not None and clock.monotonic() - started >= stop_after

        failures = 0
        while self.universe is None:
            if stopping():
                return
            try:
                await self.discover()
            except KalshiError as exc:
                failures += 1
                self._outage("discovery", exc)
                await self._sleep(_backoff(failures))
        failures = 0
        next_discovery = clock.monotonic() + config.discovery_interval
        next_screen = clock.monotonic() + config.screen_interval
        background: asyncio.Task[object] | None = None
        background_kind = ""
        rescreen = True
        try:
            while not stopping():
                now = clock.monotonic()
                if background is not None and background.done():
                    error = background.exception()
                    if error is None:
                        failures = 0
                        rescreen = True
                        done = clock.monotonic()
                        next_screen = done + config.screen_interval
                        if background_kind == "discovery":
                            next_discovery = done + config.discovery_interval
                    elif isinstance(error, KalshiError):
                        failures += 1
                        self._outage(background_kind, error)
                        retry = clock.monotonic() + _backoff(failures)
                        if background_kind == "discovery":
                            next_discovery = retry
                        else:
                            next_screen = retry
                    else:
                        raise error
                    background = None
                if background is None:
                    if now >= next_discovery:
                        background, background_kind = (
                            asyncio.create_task(self.discover()),
                            "discovery",
                        )
                    elif now >= next_screen:
                        background = asyncio.create_task(self.refresh_listings())
                        background_kind = "listing refresh"
                on_cycle(await self.cycle(rescreen=rescreen))
                rescreen = False
                await self._sleep(config.confirm_interval)
        finally:
            if background is not None and not background.done():
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError, KalshiError):
                    await background

    def _outage(self, what: str, error: BaseException) -> None:
        moment = self.client.clock.now()
        self.outages.append(f"{moment:%H:%M:%S} {what} failed: {error}")
        self.progress(f"{what} failed, retrying: {error}")


def _backoff(failures: int) -> float:
    """Seconds to wait after ``failures`` consecutive background failures: 15 s doubling to 5 min."""
    return float(min(300, 15 * 2 ** (failures - 1)))
