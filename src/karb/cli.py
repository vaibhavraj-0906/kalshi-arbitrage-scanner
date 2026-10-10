"""The ``karb`` command line.

Scanning: ``scan``, ``universe``, ``events``, ``audit``, ``explain``.
Research on recordings: ``runs``, ``replay``, ``stats``, ``sensitivity``, ``history``.
Trading on Kalshi's demo exchange: ``account``, ``order``, ``trade``, ``settle``, ``pnl``.
Reporting: ``report``.
Offline tour: ``demo`` (see docs/guide.md).

Scanning reads public market data. Trading signs orders with your demo API key and sends them to
Kalshi's demo exchange, which runs on mock funds; karb refuses to sign a request for any other
host (docs/decisions/ADR-0010).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from karb.arb.detect import DetectConfig, Detection, EventSnapshot, detect
from karb.arb.screen import ScreenHit, screen_event
from karb.core.clock import SystemClock
from karb.core.fixed import Cash, FixedPointError, Price, Qty
from karb.dashboard import collect_report, render_report
from karb.demo import run_demo
from karb.exchange.client import DEFAULT_BASE_URL, ClientStats, KalshiClient, KalshiError
from karb.exchange.endpoints import (
    fetch_event,
    fetch_exchange_status,
    fetch_one_series,
    fetch_orderbooks,
    iter_events,
    pack_batches,
    trading_shards,
)
from karb.history import HistoryScreen, fetch_candles, minute_snapshots, screen_history
from karb.market.book import OrderBook, Side
from karb.market.fees import (
    CENT_BALANCE_UNIT,
    CENTICENT_BALANCE_UNIT,
    FeeConfig,
    RoundingMode,
    resolve_fee_schedule,
)
from karb.render import (
    audit_view,
    explain_view,
    opportunities_table,
    opportunity_record,
    status_line,
    universe_view,
)
from karb.reports import (
    account_view,
    history_view,
    order_view,
    orders_view,
    plan_view,
    pnl_view,
    replay_view,
    runs_table,
    sensitivity_table,
    settle_view,
    stats_view,
    trade_view,
    trader_view,
)
from karb.scanner.service import CycleReport, ScanConfig, Scanner
from karb.store.codec import detect_config_from_json
from karb.store.database import LIVE_SOURCE, RecordStore, StoreError, new_id
from karb.store.recorder import Recorder
from karb.store.replay import compare_with_live, replay_run
from karb.store.stats import fee_sensitivity, run_statistics
from karb.structure.classify import (
    Classification,
    classify_event,
    split_by_participant,
    tradeable_tickers,
)
from karb.trading.auth import DEMO_BASE_URL, Credentials, CredentialsError
from karb.trading.engine import Trader
from karb.trading.exercise import complete_set, fetch_event_snapshots
from karb.trading.orders import Order, parse_fill
from karb.trading.plan import TradeConfig, TradePlan
from karb.trading.portfolio import fetch_balance, fetch_positions, place_orders
from karb.trading.settle import EXCHANGE_SESSIONS, settle_open_trades

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Structural arbitrage scanner for Kalshi event contracts. Scans public data; trades on "
        "Kalshi's demo exchange (mock funds) only."
    ),
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
MinApr = Annotated[
    float | None,
    typer.Option(help="Ignore baskets whose annualised edge is below this, e.g. 0.05 for 5%."),
]
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
MaxTradeCost = Annotated[str, typer.Option(help="Budget per basket in dollars, fees included.")]
Capital = Annotated[str, typer.Option(help="Cash that open positions may tie up, in dollars.")]
NoHedge = Annotated[
    bool, typer.Option("--no-hedge", help="Hold whatever filled instead of repairing it.")
]
Session = Annotated[
    str | None, typer.Option("--session", help="Only this trading session (see `karb pnl`).")
]
Yes = Annotated[bool, typer.Option("--yes", help="Send without asking for confirmation.")]
Demo = Annotated[
    bool, typer.Option("--demo", help="Read Kalshi's demo exchange instead of the real one.")
]


def _base_url(demo: bool) -> str:
    return DEMO_BASE_URL if demo else DEFAULT_BASE_URL


def _trade_config(
    max_trade_cost: str, capital: str, no_hedge: bool, max_trades: int | None = None
) -> TradeConfig:
    try:
        return TradeConfig(
            max_cost_per_trade=Cash.parse(max_trade_cost),
            capital=Cash.parse(capital),
            hedge=not no_hedge,
            max_trades=max_trades,
        )
    except (FixedPointError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


def _credentials() -> Credentials:
    try:
        return Credentials.from_env()
    except CredentialsError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _detect_config(
    min_profit: str,
    rounding: RoundingMode,
    direct_member: bool,
    taker_coefficient: str,
    levels: int,
    min_apr: float | None = None,
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
    return DetectConfig(fees=fees, min_profit=profit, max_levels=levels, min_apr=min_apr)


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
    duration: Annotated[
        float | None, typer.Option(help="Stop after this many seconds (continuous mode).")
    ] = None,
    min_profit: MinProfit = "0.01",
    rounding: Rounding = RoundingMode.WORST_CASE,
    direct_member: DirectMember = False,
    taker_coefficient: TakerCoefficient = "0.07",
    levels: Levels = 10,
    min_apr: MinApr = None,
    assert_exhaustive: AssertExhaustive = None,
    rate: Rate = 8.0,
) -> None:
    """Scan for structural arbitrage: discover, screen, confirm against live order books."""
    config = ScanConfig(
        detect=_detect_config(
            min_profit, rounding, direct_member, taker_coefficient, levels, min_apr
        ),
        series=tuple(series or ()),
        max_event_pages=max_pages,
        asserted_exhaustive=frozenset(assert_exhaustive or ()),
        watchlist_size=watchlist,
        confirm_all=confirm_all,
        confirmations=confirmations,
    )
    _run_scan(
        config,
        once=once,
        as_json=as_json,
        show_candidates=show_candidates,
        rate=rate,
        record=record,
        duration=duration,
    )


@dataclass(frozen=True, slots=True)
class _Trading:
    credentials: Credentials
    config: TradeConfig
    stop_file: Path | None


def _run_scan(config: ScanConfig, **options: object) -> None:
    try:
        asyncio.run(_scan(config, **options))  # type: ignore[arg-type]
    except KeyboardInterrupt:
        err_console.print("stopped")
    except KalshiError as exc:
        err_console.print(f"[red]Kalshi API unavailable:[/red] {exc}")
        raise typer.Exit(1) from exc
    except (StoreError, CredentialsError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _cycle_hook(
    scanner: Scanner, recorder: Recorder | None, trader: Trader | None
) -> Callable[[CycleReport], None]:
    def handle(report: CycleReport) -> None:
        if recorder is None:
            return
        cycle_no = recorder.record_cycle(
            report, payloads=scanner.event_payloads, series=scanner.series
        )
        if trader is not None:
            trader.consider(report, cycle_no)

    return handle


async def _scan(
    config: ScanConfig,
    *,
    once: bool,
    as_json: bool,
    show_candidates: bool,
    rate: float,
    record: Path | None,
    duration: float | None,
    base_url: str = DEFAULT_BASE_URL,
    trading: _Trading | None = None,
) -> None:
    store = None if record is None else RecordStore(record)
    recorder: Recorder | None = None
    trader: Trader | None = None
    try:
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(KalshiClient(base_url=base_url, rate=rate))
            if store is not None:
                recorder = Recorder(store, client.clock)
                run_id = recorder.start_run(config)
                err_console.print(f"recording run {run_id} to {record}")
                if trading is not None:
                    signed = await stack.enter_async_context(
                        KalshiClient(base_url=base_url, rate=rate, credentials=trading.credentials)
                    )
                    trader = Trader(
                        signed,
                        store,
                        run_id=run_id,
                        detect_config=config.detect,
                        config=trading.config,
                        environment="demo",
                        stop_file=trading.stop_file,
                    )
                    err_console.print(
                        f"trading on Kalshi's demo exchange (mock funds) as session "
                        f"{trader.session_id}; create {trading.stop_file} to halt trading"
                    )
            try:
                await _scan_loop(
                    client,
                    config,
                    recorder,
                    trader,
                    once=once,
                    as_json=as_json,
                    show_candidates=show_candidates,
                    record_payloads=store is not None,
                    duration=duration,
                )
            except asyncio.CancelledError:  # Ctrl+C: send nothing new
                if trader is not None:
                    trader.cancel_queued()
                raise
            finally:
                if trader is not None:
                    if trader.pending:
                        # Never abandon a basket half-filled: finish the trade already sent.
                        err_console.print(f"finishing {trader.pending} trade(s) before exiting")
                        await asyncio.shield(trader.drain())
                    err_console.print(trader_view(trader))
    finally:
        if recorder is not None and recorder.run_id is not None:
            recorder.finish_run()
            err_console.print(f"recorded {recorder.cycles:,} cycles as run {recorder.run_id}")
        if store is not None:
            store.close()


async def _scan_loop(
    client: KalshiClient,
    config: ScanConfig,
    recorder: Recorder | None,
    trader: Trader | None,
    *,
    once: bool,
    as_json: bool,
    show_candidates: bool,
    record_payloads: bool,
    duration: float | None,
) -> None:
    if once:
        with err_console.status("Starting", spinner="dots") as status:
            scanner = Scanner(
                client, config, progress=status.update, record_payloads=record_payloads
            )
            report = await scanner.run_once(on_cycle=_cycle_hook(scanner, recorder, trader))
            if trader is not None:
                status.update(f"Finishing {trader.pending} trade(s)")
                await trader.drain()
        _emit_final(scanner, report, client.stats, as_json=as_json, show_candidates=show_candidates)
        return

    scanner = Scanner(client, config, record_payloads=record_payloads)
    record_cycle = _cycle_hook(scanner, recorder, trader)
    printed: set[str] = set()
    last_view: list[RenderableType] = [Text("Starting")]
    with Live(
        Text("Starting"), console=err_console if as_json else console, refresh_per_second=4
    ) as live:
        scanner.progress = lambda message: live.update(Text(message))

        def on_cycle(report: CycleReport) -> None:
            record_cycle(report)
            table = opportunities_table(
                scanner.tracker, now=report.finished_at, show_unconfirmed=show_candidates
            )
            view = Group(
                table,
                status_line(report, client.stats, scanner.tracker, outages=len(scanner.outages)),
            )
            last_view[0] = view
            live.update(view)
            if not as_json:
                return
            for sighting in scanner.tracker.live():
                opportunity = sighting.opportunity
                if scanner.tracker.is_confirmed(sighting) and opportunity.id not in printed:
                    printed.add(opportunity.id)
                    record_json = opportunity_record(opportunity, sighting, confirmed=True)
                    typer.echo(json.dumps(record_json))

        await scanner.run_forever(on_cycle, stop_after=duration)
        if trader is not None and trader.pending:
            note = Text(f"Finishing {trader.pending} trade(s)", style="dim")
            live.update(Group(last_view[0], note))
            await trader.drain()
        live.update(last_view[0])


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
def events(
    series: Annotated[
        list[str], typer.Option("--series", "-s", help="Series to list, e.g. KXINX (repeatable).")
    ],
    limit: Annotated[int, typer.Option(help="Most events to show per series.")] = 20,
    demo: Demo = False,
    rate: Rate = 8.0,
) -> None:
    """List open events in a series, with how karb classifies each: tickers for audit/explain."""

    async def run() -> None:
        async with KalshiClient(base_url=_base_url(demo), rate=rate) as client:
            table = Table(title="Open events", header_style="bold")
            for name in ("Event", "Title", "Markets", "Structure"):
                table.add_column(name)
            for scope in series:
                info = await fetch_one_series(client, scope)
                shown = 0
                async for event in iter_events(client, series_ticker=scope, max_pages=1):
                    groups = split_by_participant(event)
                    verdicts = []
                    for group in groups:
                        result = classify_event(group, info)
                        if result.structure is not None:
                            verdicts.append(result.structure.kind.value)
                        elif result.exclusion is not None:
                            verdicts.append(f"excluded: {result.exclusion.value}")
                    summary = ", ".join(sorted(set(verdicts)))
                    if len(groups) > 1:
                        summary = f"{len(groups)} groups: {summary}"
                    table.add_row(
                        event.event_ticker, event.title[:48], str(len(event.markets)), summary
                    )
                    shown += 1
                    if shown >= limit:
                        break
            if not table.rows:
                table.caption = "No open events in those series."
            console.print(table)

    _run(run())


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
    event_ticker: str,
    config: DetectConfig,
    asserted: frozenset[str],
    rate: float,
    base_url: str = DEFAULT_BASE_URL,
) -> list[_Inspection]:
    """Classify and price one event -- each participant group separately -- on live books."""
    async with KalshiClient(base_url=base_url, rate=rate) as client:
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
    min_apr: MinApr = None,
    assert_exhaustive: AssertExhaustive = None,
    demo: Demo = False,
    rate: Rate = 8.0,
) -> None:
    """Show how one event is modelled: intervals, outcome spaces, fees, screens, LP, verdict."""
    config = _detect_config(min_profit, rounding, direct_member, taker_coefficient, levels, min_apr)

    async def run() -> None:
        inspections = await _inspect(
            event_ticker, config, frozenset(assert_exhaustive or ()), rate, _base_url(demo)
        )
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
    min_apr: MinApr = None,
    assert_exhaustive: AssertExhaustive = None,
    demo: Demo = False,
    rate: Rate = 8.0,
) -> None:
    """Price one event's best basket fill by fill, with its payoff in every outcome."""
    config = _detect_config(min_profit, rounding, direct_member, taker_coefficient, levels, min_apr)

    async def run() -> None:
        inspections = await _inspect(
            event_ticker, config, frozenset(assert_exhaustive or ()), rate, _base_url(demo)
        )
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
    min_apr: float | None = None,
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
        if min_apr is not None:
            config = replace(config, min_apr=min_apr)
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
    min_apr: MinApr = None,
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
            min_apr=min_apr,
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


# ---- trading on Kalshi's demo exchange ----------------------------------------------------------


@app.command()
def account(
    fills: Annotated[int, typer.Option(help="Recent fills to show.")] = 10,
    rate: Rate = 8.0,
) -> None:
    """Your Kalshi demo account: balance, open positions and recent fills. Checks your API key."""
    credentials = _credentials()

    async def run() -> None:
        async with KalshiClient(
            base_url=DEMO_BASE_URL, rate=rate, credentials=credentials
        ) as client:
            balance = await fetch_balance(client)
            positions = await fetch_positions(client, None)
            payload = await client.get("/portfolio/fills", [("limit", fills)], auth=True)
            recent: list[tuple[str, str]] = []
            for raw in payload.get("fills") or []:
                fill = parse_fill(raw)
                recent.append(
                    (
                        str(raw.get("created_time") or ""),
                        f"{fill.ticker}: bought {fill.qty} {fill.side.value.upper()} at "
                        f"{fill.price}, fee {fill.fee.dollars(6)}",
                    )
                )
        console.print(f"signed in as key {credentials.key_id[:8]}... ({credentials.algorithm})")
        console.print(account_view(balance, positions, recent))

    _run(run())


@app.command()
def order(
    ticker: Annotated[str, typer.Argument(help="Market ticker on the demo exchange.")],
    side: Annotated[Side, typer.Option(help="Which side to buy.")],
    limit: Annotated[str, typer.Option(help="Most to pay per contract, in dollars.")],
    qty: Annotated[str, typer.Option(help="Contracts to buy.")] = "1",
    yes: Yes = False,
    rate: Rate = 8.0,
) -> None:
    """Send one immediate-or-cancel buy to Kalshi's demo exchange and show its fills."""
    try:
        wanted = Order(ticker, side, Price.parse(limit), Qty.parse(qty))
    except FixedPointError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if wanted.qty.is_zero:
        raise typer.BadParameter("buy at least 0.01 contracts")
    credentials = _credentials()
    console.print(
        f"buy {wanted.qty} {side.value.upper()} of {ticker} at no more than {wanted.limit}, "
        "immediate-or-cancel, on Kalshi's demo exchange (mock funds)"
    )
    if not yes and not typer.confirm("Send it?"):
        console.print("nothing sent")
        return

    async def run() -> None:
        async with KalshiClient(
            base_url=DEMO_BASE_URL, rate=rate, credentials=credentials
        ) as client:
            market = await client.get(f"/markets/{ticker}")
            event = await fetch_event(client, str(market["market"]["event_ticker"]))
            series = await fetch_one_series(client, event.series_ticker)
            schedule = resolve_fee_schedule(event, series).schedule
            now = client.clock.now()
            placed = await place_orders(
                client,
                [wanted],
                trade_id=f"order-{new_id(now)}",
                phase="manual",
                since=now - timedelta(seconds=5),
            )
        model = Cash.ZERO if schedule is None else placed[0].model_fees(schedule, FeeConfig())
        console.print(order_view(placed[0], model))

    _run(run())


@app.command()
def trade(
    db: Annotated[
        Path, typer.Option("--db", help="Recording database; every order and trade is kept here.")
    ] = Path("data/trading.duckdb"),
    exercise: Annotated[
        str | None,
        typer.Option(
            "--exercise",
            help="Instead of scanning, buy complete sets on this event to exercise trading.",
        ),
    ] = None,
    sets: Annotated[int, typer.Option(help="Complete sets to buy with --exercise.")] = 1,
    yes: Yes = False,
    once: Annotated[bool, typer.Option("--once", help="One full pass, then exit.")] = False,
    duration: Annotated[float | None, typer.Option(help="Stop after this many seconds.")] = None,
    series: Series = None,
    max_pages: MaxPages = None,
    watchlist: Annotated[
        int, typer.Option(help="Most liquid eligible events confirmed each cycle.")
    ] = 40,
    max_trade_cost: MaxTradeCost = "100",
    capital: Capital = "10000",
    max_trades: Annotated[
        int | None, typer.Option(help="Stop trading after this many trades.")
    ] = None,
    no_hedge: NoHedge = False,
    stop_file: Annotated[
        Path, typer.Option(help="Trading halts as soon as this file exists.")
    ] = Path("data/STOP"),
    min_profit: MinProfit = "0.01",
    rounding: Rounding = RoundingMode.WORST_CASE,
    direct_member: DirectMember = False,
    taker_coefficient: TakerCoefficient = "0.07",
    levels: Levels = 10,
    min_apr: MinApr = None,
    assert_exhaustive: AssertExhaustive = None,
    rate: Rate = 8.0,
) -> None:
    """Scan Kalshi's demo exchange and trade every verified opportunity there (mock funds)."""
    credentials = _credentials()
    detect_config = _detect_config(
        min_profit, rounding, direct_member, taker_coefficient, levels, min_apr
    )
    trade_config = _trade_config(max_trade_cost, capital, no_hedge, max_trades)
    if exercise is not None:
        _exercise(exercise, sets, yes, db, credentials, detect_config, trade_config, rate)
        return
    config = ScanConfig(
        detect=detect_config,
        series=tuple(series or ()),
        max_event_pages=max_pages,
        asserted_exhaustive=frozenset(assert_exhaustive or ()),
        watchlist_size=watchlist,
        confirmations=1,
    )
    _run_scan(
        config,
        once=once,
        as_json=False,
        show_candidates=False,
        rate=rate,
        record=db,
        duration=duration,
        base_url=DEMO_BASE_URL,
        trading=_Trading(credentials, trade_config, stop_file),
    )


def _exercise(
    event_ticker: str,
    sets: int,
    yes: bool,
    db: Path,
    credentials: Credentials,
    detect_config: DetectConfig,
    trade_config: TradeConfig,
    rate: float,
) -> None:
    async def prepare() -> tuple[EventSnapshot, TradePlan] | list[str]:
        async with KalshiClient(base_url=DEMO_BASE_URL, rate=rate) as client:
            snapshots, reasons = await fetch_event_snapshots(
                client, event_ticker, max_levels=detect_config.max_levels
            )
        for snapshot in snapshots:
            plan = complete_set(snapshot, sets=sets, fee_config=detect_config.fees)
            if not isinstance(plan, str):
                return snapshot, plan
            reasons.append(f"{snapshot.structure.event.event_ticker}: {plan}")
        return reasons

    try:
        prepared = asyncio.run(prepare())
    except KalshiError as exc:
        err_console.print(f"[red]Kalshi API error:[/red] {exc}")
        raise typer.Exit(1) from exc
    if isinstance(prepared, list):
        err_console.print(f"nothing to exercise on {event_ticker}:")
        for reason in prepared or ["no scannable groups"]:
            err_console.print(f"  {reason}")
        raise typer.Exit(1)
    snapshot, plan = prepared
    if plan.planned_cost > trade_config.max_cost_per_trade:
        err_console.print(
            f"{sets} set(s) cost {plan.planned_cost.dollars()}, over the "
            f"{trade_config.max_cost_per_trade.dollars()} per-trade budget (--max-trade-cost)"
        )
        raise typer.Exit(1)
    console.print(plan_view(plan))
    if not yes and not typer.confirm("Send these orders to Kalshi's demo exchange (mock funds)?"):
        console.print("nothing sent")
        return

    async def send() -> None:
        with RecordStore(db) as store:
            async with KalshiClient(
                base_url=DEMO_BASE_URL, rate=rate, credentials=credentials
            ) as client:
                trader = Trader(
                    client,
                    store,
                    run_id="exercise",
                    detect_config=detect_config,
                    config=trade_config,
                    environment="demo",
                )
                outcome = await trader.execute(plan, snapshot)
            console.print(trader_view(trader))
            if outcome is not None:
                console.print(trade_view(outcome))
                console.print(f"recorded in {db}: karb pnl --db {db}")

    _run(send())


@app.command()
def settle(db: Database = DEFAULT_DB, session: Session = None, rate: Rate = 8.0) -> None:
    """Settle open trades whose markets have finalized, and audit them against the model."""

    async def run() -> None:
        with _open_store(db, write=True) as store:
            open_sessions = {t.session_id for t in store.trades(session_id=session, status="open")}
            sessions = [s for s in store.sessions() if s.session_id in open_sessions]
            if not sessions:
                console.print("no open trades to settle")
                return
            credentials: Credentials | None = None
            asked = False
            for row in sessions:
                if row.kind == "simulated":
                    console.print(
                        f"session {row.session_id} traded a simulated exchange: karb demo settles it"
                    )
                    continue
                base_url = DEFAULT_BASE_URL
                if row.kind in EXCHANGE_SESSIONS:
                    base_url = DEMO_BASE_URL
                    if not asked:
                        asked = True
                        try:
                            credentials = Credentials.from_env()
                        except CredentialsError:
                            console.print(
                                "no demo API key set: settling from public results, without "
                                "checking the exchange's settlement records"
                            )
                async with KalshiClient(
                    base_url=base_url,
                    rate=rate,
                    credentials=credentials if row.kind in EXCHANGE_SESSIONS else None,
                ) as client:
                    summary = await settle_open_trades(store, client, session_id=row.session_id)
                console.print(f"session {row.session_id}:")
                console.print(settle_view(summary))

    _run(run())


@app.command()
def pnl(
    db: Database = DEFAULT_DB,
    session: Session = None,
    orders: Annotated[
        bool, typer.Option("--orders", help="Also show every order as the exchange recorded it.")
    ] = False,
) -> None:
    """Trades and their P&L attribution: planned, execution, repair, settlement."""
    with _open_store(db) as store:
        trades = store.trades(session_id=session)
        console.print(pnl_view(trades, store.sessions()))
        if orders:
            for row in trades:
                console.print(orders_view(row, store.trade_orders(row.trade_id)))


@app.command()
def report(
    run_id: RunId = None,
    db: Database = DEFAULT_DB,
    out: Annotated[
        Path | None, typer.Option("--out", help="Where to write the HTML report.")
    ] = None,
    sensitivity: Annotated[
        bool,
        typer.Option("--sensitivity/--no-sensitivity", help="Replay under each fee scenario."),
    ] = True,
) -> None:
    """Write a self-contained HTML research report for a recorded run."""
    with _open_store(db) as store:
        resolved = _resolve_run(store, run_id)
        with err_console.status("Building the report"):
            data = collect_report(
                store, resolved, generated_at=SystemClock().now(), sensitivity=sensitivity
            )
    target = out or db.with_name(f"report-{resolved}.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(data), encoding="utf-8")
    console.print(f"wrote {target}")


@app.command()
def demo(
    db: Annotated[Path, typer.Option("--db", help="Where to write the demo recording.")] = Path(
        "data/demo.duckdb"
    ),
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Replace an existing demo recording.")
    ] = False,
) -> None:
    """Build an offline demo recording: scan a simulated exchange, trade it, settle."""
    if db.exists():
        if not overwrite:
            err_console.print(f"{db} exists; pass --overwrite to replace it")
            raise typer.Exit(1)
        with _open_store(db):
            pass  # refuse to delete anything that is not a karb recording
        db.unlink()
        db.with_name(db.name + ".wal").unlink(missing_ok=True)
    summary = asyncio.run(run_demo(db))
    settlement = summary.settlement
    lines = [
        f"demo recording written to {db}",
        f"  run {summary.run_id}: {summary.cycles} cycles over 3 simulated events",
        f"  trading session {summary.session_id}: {summary.trades} trades, "
        f"{settlement.settled} settled, realized {summary.realized.dollars()}",
        f"  simulated account balance {summary.balance.dollars()} (from $10,000.00)",
        "next: karb runs | replay | stats | sensitivity | pnl | report "
        f"--db {db}  (walkthrough: docs/guide.md)",
    ]
    if summary.halted:
        lines.insert(3, f"  TRADING HALTED: {summary.halted}")
    console.print("\n".join(lines))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
