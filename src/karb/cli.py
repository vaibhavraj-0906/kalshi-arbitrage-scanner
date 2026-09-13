"""The ``karb`` command line: scan, universe, audit, explain.

Public market data only. Nothing here can place an order; there is no code that could.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Annotated

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from karb.arb.detect import DetectConfig, Detection, EventSnapshot, detect
from karb.arb.screen import ScreenHit, screen_event
from karb.core.fixed import Cash, FixedPointError
from karb.exchange.client import ClientStats, KalshiClient, KalshiError
from karb.exchange.endpoints import (
    fetch_event,
    fetch_exchange_status,
    fetch_one_series,
    fetch_orderbooks,
    pack_batches,
    trading_shards,
)
from karb.market.book import OrderBook
from karb.market.fees import CENT_BALANCE_UNIT, CENTICENT_BALANCE_UNIT, FeeConfig, RoundingMode
from karb.render import (
    audit_view,
    explain_view,
    opportunities_table,
    opportunity_record,
    status_line,
    universe_view,
)
from karb.scanner.service import CycleReport, ScanConfig, Scanner
from karb.structure.classify import (
    Classification,
    classify_event,
    split_by_participant,
    tradeable_tickers,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Structural arbitrage scanner for Kalshi event contracts. Public data, research only.",
)
console = Console()
err_console = Console(stderr=True)

MinProfit = Annotated[
    str, typer.Option(help="Smallest guaranteed P&L to report, in dollars, after fees.")
]
Rounding = Annotated[
    RoundingMode, typer.Option(help="Fee rounding model: worst_case never assumes a rebate.")
]
DirectMember = Annotated[
    bool, typer.Option("--direct-member", help="Align balances to $0.0001 instead of $0.01.")
]
TakerCoefficient = Annotated[
    str, typer.Option(help="Taker fee coefficient before series multipliers.")
]
Levels = Annotated[int, typer.Option(help="Order book levels per side considered when sizing.")]
AssertExhaustive = Annotated[
    list[str] | None,
    typer.Option(
        help="Event or series ticker whose listed outcomes you vouch are complete (repeatable)."
    ),
]
Rate = Annotated[float, typer.Option(help="Requests per second to the public API.")]
Series = Annotated[
    list[str] | None, typer.Option("--series", "-s", help="Only these series (repeatable).")
]
MaxPages = Annotated[int | None, typer.Option(help="Cap discovery at N pages of 200 events.")]


def _detect_config(
    min_profit: str,
    rounding: RoundingMode,
    direct_member: bool,
    taker_coefficient: str,
    levels: int,
) -> DetectConfig:
    try:
        profit = Cash.parse(min_profit)
        coefficient = Fraction(Decimal(taker_coefficient))
    except (FixedPointError, InvalidOperation) as exc:
        raise typer.BadParameter(str(exc)) from exc
    fees = FeeConfig(
        taker_coefficient=coefficient,
        balance_unit=CENTICENT_BALANCE_UNIT if direct_member else CENT_BALANCE_UNIT,
        rounding_mode=rounding,
    )
    return DetectConfig(fees=fees, min_profit=profit, max_levels=levels)


@app.command()
def scan(
    once: Annotated[bool, typer.Option("--once", help="One full pass, then exit.")] = False,
    series: Series = None,
    max_pages: MaxPages = None,
    watchlist: Annotated[
        int, typer.Option(help="Most liquid eligible events confirmed each cycle.")
    ] = 40,
    confirm_all: Annotated[
        bool, typer.Option("--confirm-all", help="Fetch books for every eligible event (slow).")
    ] = False,
    confirmations: Annotated[
        int, typer.Option(help="Consecutive sightings before an opportunity is confirmed.")
    ] = 2,
    show_candidates: Annotated[
        bool, typer.Option("--show-candidates", help="Also show not-yet-confirmed sightings.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit opportunities as JSON lines.")
    ] = False,
    min_profit: MinProfit = "0.01",
    rounding: Rounding = RoundingMode.WORST_CASE,
    direct_member: DirectMember = False,
    taker_coefficient: TakerCoefficient = "0.07",
    levels: Levels = 10,
    assert_exhaustive: AssertExhaustive = None,
    rate: Rate = 8.0,
) -> None:
    """Scan for structural arbitrage: discover, screen, confirm against live order books."""
    config = ScanConfig(
        detect=_detect_config(min_profit, rounding, direct_member, taker_coefficient, levels),
        series=tuple(series or ()),
        max_event_pages=max_pages,
        asserted_exhaustive=frozenset(assert_exhaustive or ()),
        watchlist_size=watchlist,
        confirm_all=confirm_all,
        confirmations=confirmations,
    )
    try:
        asyncio.run(
            _scan(config, once=once, as_json=as_json, show_candidates=show_candidates, rate=rate)
        )
    except KeyboardInterrupt:
        err_console.print("stopped")
    except KalshiError as exc:
        err_console.print(f"[red]Kalshi API unavailable:[/red] {exc}")
        raise typer.Exit(1) from exc


async def _scan(
    config: ScanConfig, *, once: bool, as_json: bool, show_candidates: bool, rate: float
) -> None:
    async with KalshiClient(rate=rate) as client:
        if once:
            with err_console.status("Starting", spinner="dots") as status:
                scanner = Scanner(client, config, progress=status.update)
                report = await scanner.run_once()
            _emit_final(
                scanner, report, client.stats, as_json=as_json, show_candidates=show_candidates
            )
            return

        scanner = Scanner(client, config)
        printed: set[str] = set()
        with Live(
            Text("Starting"), console=err_console if as_json else console, refresh_per_second=4
        ) as live:
            scanner.progress = lambda message: live.update(Text(message))

            def on_cycle(report: CycleReport) -> None:
                table = opportunities_table(
                    scanner.tracker, now=report.finished_at, show_unconfirmed=show_candidates
                )
                live.update(Group(table, status_line(report, client.stats, scanner.tracker)))
                if not as_json:
                    return
                for sighting in scanner.tracker.live():
                    opportunity = sighting.opportunity
                    if scanner.tracker.is_confirmed(sighting) and opportunity.id not in printed:
                        printed.add(opportunity.id)
                        record = opportunity_record(opportunity, sighting, confirmed=True)
                        typer.echo(json.dumps(record))

            await scanner.run_forever(on_cycle)


def _emit_final(
    scanner: Scanner,
    report: CycleReport,
    stats: ClientStats,
    *,
    as_json: bool,
    show_candidates: bool,
) -> None:
    tracker = scanner.tracker
    if as_json:
        for sighting in tracker.live():
            confirmed = tracker.is_confirmed(sighting)
            if confirmed or show_candidates:
                record = opportunity_record(sighting.opportunity, sighting, confirmed=confirmed)
                typer.echo(json.dumps(record))
        return
    console.print(
        opportunities_table(tracker, now=report.finished_at, show_unconfirmed=show_candidates)
    )
    console.print(status_line(report, stats, tracker))
    universe = scanner.universe
    if universe is not None:
        console.print(
            Text(
                f"{universe.events_seen:,} open events in {universe.groups_seen:,} groups, "
                f"{len(universe.structures):,} with exploitable structure, "
                f"{report.untradeable:,} of those lacking two tradeable markets; "
                f"{len(report.targets):,} confirmed against order books.",
                style="dim",
            )
        )
    for issue in (report.fetch_errors + report.integrity)[:10]:
        err_console.print(f"[yellow]integrity:[/yellow] {issue}")


@app.command()
def universe(
    series: Series = None,
    max_pages: MaxPages = None,
    assert_exhaustive: AssertExhaustive = None,
    rate: Rate = 8.0,
) -> None:
    """Classify every open event and explain exactly what is and is not scannable."""
    config = ScanConfig(
        series=tuple(series or ()),
        max_event_pages=max_pages,
        asserted_exhaustive=frozenset(assert_exhaustive or ()),
    )

    async def run() -> None:
        async with KalshiClient(rate=rate) as client:
            with err_console.status("Starting", spinner="dots") as status:
                scanner = Scanner(client, config, progress=status.update)
                discovered = await scanner.discover()
                hits, _targets, screened, untradeable = scanner.screen(client.clock.now())
            console.print(universe_view(discovered))
            console.print(
                f"{screened:,} scannable groups have two or more tradeable markets right now "
                f"({untradeable:,} do not); {len(hits):,} pass a top-of-book screen."
            )
            if scanner.skips.total:
                console.print(f"skipped malformed records: {dict(scanner.skips.counts)}")
            stats = client.stats
            console.print(
                Text(
                    f"{discovered.series_count:,} series loaded | requests {stats.requests:,} "
                    f"(retries {stats.retries:,}, resets {stats.resets:,}, 429s {stats.throttled})",
                    style="dim",
                )
            )

    _run(run())


@dataclass
class _Inspection:
    classification: Classification
    tradeable: frozenset[str]
    books: dict[str, OrderBook]
    hits: list[ScreenHit]
    detection: Detection | None


async def _inspect(
    event_ticker: str, config: DetectConfig, asserted: frozenset[str], rate: float
) -> list[_Inspection]:
    """Classify and price one event -- each participant group separately -- on live books."""
    async with KalshiClient(rate=rate) as client:
        shards = trading_shards(await fetch_exchange_status(client))
        event = await fetch_event(client, event_ticker)
        series = await fetch_one_series(client, event.series_ticker)
        books: dict[str, OrderBook] = {}
        sent = received = client.clock.monotonic()
        for batch in pack_batches([sorted(event.tickers)]):
            fetched = await fetch_orderbooks(client, batch, depth=config.max_levels)
            books.update(fetched.books)
            received = fetched.received_at
        now = client.clock.now()

        inspections: list[_Inspection] = []
        for group in split_by_participant(event):
            classification = classify_event(group, series, asserted_exhaustive=asserted)
            structure = classification.structure
            if structure is None:
                inspections.append(_Inspection(classification, frozenset(), {}, [], None))
                continue
            tradeable = tradeable_tickers(group, now=now, trading_shards=shards)
            quotes = {market.ticker: market.quote for market in group.markets}
            snapshot = EventSnapshot(structure, books, tradeable, now, received - sent)
            inspections.append(
                _Inspection(
                    classification,
                    tradeable,
                    books,
                    screen_event(structure, quotes, tradeable),
                    detect(snapshot, config),
                )
            )
        return inspections


def _run(coroutine: object) -> None:
    try:
        asyncio.run(coroutine)  # type: ignore[arg-type]
    except KalshiError as exc:
        err_console.print(f"[red]Kalshi API error:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command()
def audit(
    event_ticker: Annotated[str, typer.Argument(help="Event ticker, e.g. KXINX-26SEP14H1600.")],
    min_profit: MinProfit = "0.01",
    rounding: Rounding = RoundingMode.WORST_CASE,
    direct_member: DirectMember = False,
    taker_coefficient: TakerCoefficient = "0.07",
    levels: Levels = 10,
    assert_exhaustive: AssertExhaustive = None,
    rate: Rate = 8.0,
) -> None:
    """Show how one event is modelled: intervals, outcome spaces, fees, screens, LP, verdict."""
    config = _detect_config(min_profit, rounding, direct_member, taker_coefficient, levels)

    async def run() -> None:
        inspections = await _inspect(event_ticker, config, frozenset(assert_exhaustive or ()), rate)
        _print_inspections(inspections, explain=False)

    _run(run())


@app.command()
def explain(
    event_ticker: Annotated[str, typer.Argument(help="Event ticker, e.g. KXINX-26SEP14H1600.")],
    min_profit: MinProfit = "0.01",
    rounding: Rounding = RoundingMode.WORST_CASE,
    direct_member: DirectMember = False,
    taker_coefficient: TakerCoefficient = "0.07",
    levels: Levels = 10,
    assert_exhaustive: AssertExhaustive = None,
    rate: Rate = 8.0,
) -> None:
    """Price one event's best basket fill by fill, with its payoff in every outcome."""
    config = _detect_config(min_profit, rounding, direct_member, taker_coefficient, levels)

    async def run() -> None:
        inspections = await _inspect(event_ticker, config, frozenset(assert_exhaustive or ()), rate)
        _print_inspections(inspections, explain=True)

    _run(run())


def _print_inspections(inspections: list[_Inspection], *, explain: bool) -> None:
    if len(inspections) > 1:
        console.print(
            f"{len(inspections)} participant groups: each is a separate ladder and modelled alone."
        )
    for inspection in inspections:
        classification = inspection.classification
        structure, detection = classification.structure, inspection.detection
        if structure is None or detection is None:
            reason = classification.exclusion.value if classification.exclusion else "unknown"
            console.print(
                f"{classification.event.event_ticker} is not scannable: {reason}. "
                f"{classification.detail}"
            )
            continue
        console.print(
            audit_view(
                structure, inspection.tradeable, inspection.books, inspection.hits, detection
            )
        )
        if not explain:
            continue
        for opportunity in detection.opportunities:
            console.print(explain_view(opportunity))
        if not detection.opportunities:
            console.print(
                "No basket guarantees a profit after fees at current depth. The LP column above "
                "shows the best guaranteed profit before fee rounding in each tier; zero means "
                "the quotes admit a consistent probability for every outcome."
            )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
