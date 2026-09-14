"""The ``karb`` command line.

Scanning: ``scan``, ``universe``, ``audit``, ``explain``.
Research on recordings: ``runs``, ``replay``, ``stats``, ``sensitivity``, ``history``.

Public market data only. Nothing here can place an order; there is no code that could.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Final

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
from karb.history import HistoryScreen, fetch_candles, minute_snapshots, screen_history
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
from karb.reports import history_view, replay_view, runs_table, sensitivity_table, stats_view
from karb.scanner.service import CycleReport, ScanConfig, Scanner
from karb.store.codec import detect_config_from_json
from karb.store.database import LIVE_SOURCE, RecordStore, StoreError
from karb.store.recorder import Recorder
from karb.store.replay import compare_with_live, replay_run
from karb.store.stats import fee_sensitivity, run_statistics
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

DEFAULT_DB: Final = Path("data/karb.duckdb")

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
Database = Annotated[Path, typer.Option("--db", help="Recording database (a DuckDB file).")]
RunId = Annotated[
    str | None, typer.Argument(help="Run id from `karb runs`. Defaults to the latest run.")
]


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


# ---- scanning -----------------------------------------------------------------------------------


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
    record: Annotated[
        Path | None,
        typer.Option("--record", help="Record every cycle to this DuckDB file for research."),
    ] = None,
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
            _scan(
                config,
                once=once,
                as_json=as_json,
                show_candidates=show_candidates,
                rate=rate,
                record=record,
            )
        )
    except KeyboardInterrupt:
        err_console.print("stopped")
    except KalshiError as exc:
        err_console.print(f"[red]Kalshi API unavailable:[/red] {exc}")
        raise typer.Exit(1) from exc
    except StoreError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _recording_hook(scanner: Scanner, recorder: Recorder | None) -> Callable[[CycleReport], None]:
    def record(report: CycleReport) -> None:
        if recorder is not None:
            recorder.record_cycle(report, payloads=scanner.event_payloads, series=scanner.series)

    return record


async def _scan(
    config: ScanConfig,
    *,
    once: bool,
    as_json: bool,
    show_candidates: bool,
    rate: float,
    record: Path | None,
) -> None:
    store = None if record is None else RecordStore(record)
    recorder: Recorder | None = None
    try:
        async with KalshiClient(rate=rate) as client:
            if store is not None:
                recorder = Recorder(store, client.clock)
                recorder.start_run(config)
                err_console.print(f"recording run {recorder.run_id} to {record}")
            if once:
                with err_console.status("Starting", spinner="dots") as status:
                    scanner = Scanner(
                        client, config, progress=status.update, record_payloads=store is not None
                    )
                    report = await scanner.run_once(on_cycle=_recording_hook(scanner, recorder))
                _emit_final(
                    scanner, report, client.stats, as_json=as_json, show_candidates=show_candidates
                )
                return

            scanner = Scanner(client, config, record_payloads=store is not None)
            record_cycle = _recording_hook(scanner, recorder)
            printed: set[str] = set()
            with Live(
                Text("Starting"), console=err_console if as_json else console, refresh_per_second=4
            ) as live:
                scanner.progress = lambda message: live.update(Text(message))

                def on_cycle(report: CycleReport) -> None:
                    record_cycle(report)
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
                            record_json = opportunity_record(opportunity, sighting, confirmed=True)
                            typer.echo(json.dumps(record_json))

                await scanner.run_forever(on_cycle)
    finally:
        if recorder is not None and recorder.run_id is not None:
            recorder.finish_run()
            err_console.print(f"recorded {recorder.cycles:,} cycles as run {recorder.run_id}")
        if store is not None:
            store.close()


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


# ---- research on recordings ---------------------------------------------------------------------


def _open_store(db: Path, *, write: bool = False) -> RecordStore:
    try:
        return RecordStore(db, read_only=not write)
    except StoreError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _resolve_run(store: RecordStore, run_id: str | None) -> str:
    resolved = run_id or store.latest_run_id()
    if resolved is None:
        err_console.print(f"no runs recorded in {store.path}")
        raise typer.Exit(1)
    try:
        store.run(resolved)
    except StoreError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    return resolved


def _override(
    recorded: DetectConfig,
    *,
    min_profit: str | None,
    rounding: RoundingMode | None,
    direct_member: bool | None,
    taker_coefficient: str | None,
    levels: int | None,
) -> DetectConfig:
    fees = recorded.fees
    config = recorded
    try:
        if taker_coefficient is not None:
            fees = replace(fees, taker_coefficient=Fraction(Decimal(taker_coefficient)))
        if rounding is not None:
            fees = replace(fees, rounding_mode=rounding)
        if direct_member is not None:
            unit = CENTICENT_BALANCE_UNIT if direct_member else CENT_BALANCE_UNIT
            fees = replace(fees, balance_unit=unit)
        config = replace(config, fees=fees)
        if min_profit is not None:
            config = replace(config, min_profit=Cash.parse(min_profit))
        if levels is not None:
            config = replace(config, max_levels=levels)
    except (FixedPointError, InvalidOperation, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    return config


@app.command()
def runs(db: Database = DEFAULT_DB) -> None:
    """List recorded runs."""
    with _open_store(db) as store:
        console.print(runs_table(store.runs()))


@app.command()
def replay(
    run_id: RunId = None,
    db: Database = DEFAULT_DB,
    save: Annotated[
        bool, typer.Option("--save", help="Store the results, for karb stats --source.")
    ] = False,
    min_profit: Annotated[
        str | None, typer.Option(help="Override the recorded minimum profit, in dollars.")
    ] = None,
    rounding: Annotated[
        RoundingMode | None, typer.Option(help="Override the recorded fee rounding model.")
    ] = None,
    direct_member: Annotated[
        bool | None,
        typer.Option("--direct-member/--no-direct-member", help="Override balance precision."),
    ] = None,
    taker_coefficient: Annotated[
        str | None, typer.Option(help="Override the recorded taker fee coefficient.")
    ] = None,
    levels: Annotated[int | None, typer.Option(help="Override the recorded book depth.")] = None,
) -> None:
    """Re-run detection over a recorded run: exactly as recorded, or under another fee model."""
    with _open_store(db, write=save) as store:
        resolved = _resolve_run(store, run_id)
        run = store.run(resolved)
        recorded = detect_config_from_json(run.config["detect"])
        config = _override(
            recorded,
            min_profit=min_profit,
            rounding=rounding,
            direct_member=direct_member,
            taker_coefficient=taker_coefficient,
            levels=levels,
        )
        asserted = frozenset(run.config.get("asserted_exhaustive", []))
        with err_console.status(f"Replaying {run.observations:,} observations"):
            outcome = replay_run(store, resolved, config, asserted_exhaustive=asserted, save=save)
        comparison = compare_with_live(store, outcome) if config == recorded else None
        console.print(replay_view(outcome, comparison))


@app.command()
def stats(
    run_id: RunId = None,
    db: Database = DEFAULT_DB,
    source: Annotated[
        str, typer.Option(help="'live', or the id of a replay saved with --save.")
    ] = LIVE_SOURCE,
) -> None:
    """How often a recorded run saw violations, how long they lasted, and how much they held."""
    with _open_store(db) as store:
        resolved = _resolve_run(store, run_id)
        console.print(stats_view(run_statistics(store, resolved, source)))


@app.command()
def sensitivity(run_id: RunId = None, db: Database = DEFAULT_DB) -> None:
    """Replay a recorded run under alternative fee models, on identical books."""
    with _open_store(db) as store:
        resolved = _resolve_run(store, run_id)
        run = store.run(resolved)
        recorded = detect_config_from_json(run.config["detect"])
        asserted = frozenset(run.config.get("asserted_exhaustive", []))
        with err_console.status("Replaying under each fee scenario"):
            rows = fee_sensitivity(store, resolved, recorded, asserted_exhaustive=asserted)
        console.print(sensitivity_table(rows))


@app.command()
def history(
    event_ticker: Annotated[str, typer.Argument(help="Event ticker, e.g. KXINX-26SEP14H1600.")],
    hours: Annotated[float, typer.Option(help="How far back to screen, in hours.")] = 24.0,
    period: Annotated[int, typer.Option(help="Candle period in minutes.")] = 1,
    rate: Rate = 8.0,
) -> None:
    """Screen an event's candle history for top-of-book violations. No depth, so no verdicts."""

    async def run() -> None:
        async with KalshiClient(rate=rate) as client:
            event = await fetch_event(client, event_ticker)
            series = await fetch_one_series(client, event.series_ticker)
            end = int(client.clock.now().timestamp())
            start = end - int(hours * 3600)
            results: list[HistoryScreen] = []
            with err_console.status("Fetching candles", spinner="dots") as status:
                for group in split_by_participant(event):
                    structure = classify_event(group, series).structure
                    if structure is None:
                        continue
                    status.update(f"Fetching candles for {group.event_ticker}")
                    candles = await fetch_candles(
                        client,
                        sorted(group.tickers),
                        start_ts=start,
                        end_ts=end,
                        period_minutes=period,
                    )
                    snapshots = minute_snapshots(candles, start_ts=start)
                    results.append(screen_history(structure, snapshots))
            if not results:
                console.print(f"{event_ticker} has no scannable groups")
                return
            console.print(history_view(results, hours=hours))

    _run(run())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
