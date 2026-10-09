"""Paper trading end to end on the mock exchange: live, settled, audited, and replayed."""

from __future__ import annotations

from pathlib import Path

from karb.arb.detect import DetectConfig
from karb.paper.live import PaperTrader
from karb.paper.replay import simulate_paper
from karb.paper.settle import settle_open_trades
from karb.paper.trade import PaperConfig
from karb.scanner.service import CycleReport, ScanConfig, Scanner
from karb.store.codec import detect_config_from_json
from karb.store.database import RecordStore
from karb.store.recorder import Recorder
from tests.integration.planted import PLANTED, PlantedExchange, make_client, record_scan
from tests.support import captured_at

A, B, C = PLANTED
SETTLE_C_WINS = {A: ("finalized", "no"), B: ("finalized", "no"), C: ("finalized", "yes")}


async def trade_live(path: Path, exchange: PlantedExchange) -> PaperTrader:
    client, sleep = make_client(exchange)
    store = RecordStore(path)
    recorder = Recorder(store, client.clock)
    async with client:
        scanner = Scanner(client, ScanConfig(watchlist_size=10), sleep=sleep, record_payloads=True)
        run_id = recorder.start_run(scanner.config)
        trader = PaperTrader(
            client,
            store,
            run_id=run_id,
            detect_config=DetectConfig(),
            config=PaperConfig(),
            sleep=sleep,
        )

        def handle(report: CycleReport) -> None:
            cycle_no = recorder.record_cycle(
                report, payloads=scanner.event_payloads, series=scanner.series
            )
            trader.consider(report, cycle_no)

        await scanner.run_once(on_cycle=handle)
        await trader.drain()
    recorder.finish_run()
    store.close()
    return trader


async def test_live_paper_trade_is_partly_filled_hedged_settled_and_audited(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    exchange = PlantedExchange(thin_on_arrival=frozenset({C}), results=SETTLE_C_WINS)
    trader = await trade_live(path, exchange)

    assert trader.errors == []
    assert exchange.arrival_fetches == 2  # one arrival book, one hedge book
    assert trader.skips == {"group already traded this run": 1}  # cycle 2 saw it again
    with RecordStore(path, read_only=True) as store:
        (trade,) = store.paper_trades()
        # Priced by hand in tests/unit/test_paper_trade.py.
        assert (trade.planned_pnl, trade.worst_after_entry, trade.worst_after_hedge) == (
            7_480_000,
            -11_680_000,
            -8_420_000,
        )
        assert (trade.status, trade.planned_contracts, trade.filled_contracts) == (
            "open",
            15_000,
            10_000,
        )
        phases = sorted({order.phase for order in store.paper_orders(trade.trade_id)})
        assert phases == ["entry", "hedge", "plan"]

    client, _ = make_client(exchange)
    with RecordStore(path) as store:
        async with client:
            summary = await settle_open_trades(store, client)
        assert (summary.settled, summary.pending, summary.violations) == (1, 0, [])
        (settled,) = store.paper_trades()
        assert settled.status == "settled"
        # NO A and NO B pay $100; the YES hedges pay nothing. $100 - $108.42.
        assert (settled.payout, settled.realized_pnl) == (100_000_000, -8_420_000)
        assert settled.model_violation is False
        # Only held markets are looked up: NO on C never filled, so C is not in the position.
        assert set(store.settlements(list(PLANTED))) == {A, B}


async def test_settlement_waits_for_finalized_results(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    exchange = PlantedExchange(
        thin_on_arrival=frozenset({C}),
        results={**SETTLE_C_WINS, A: ("determined", "no")},  # A is held; C is not
    )
    await trade_live(path, exchange)
    client, _ = make_client(exchange)
    with RecordStore(path) as store:
        async with client:
            summary = await settle_open_trades(store, client)
        assert (summary.settled, summary.pending) == (0, 1)
        assert store.paper_trades()[0].status == "open"


async def test_replayed_paper_trading_and_a_model_violation(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await record_scan(path, confirmations=3)
    with RecordStore(path) as store:
        recorded = detect_config_from_json(store.run(run_id).config["detect"])
        result = simulate_paper(store, run_id, recorded, PaperConfig(), now=captured_at())
        assert result.decisions == 3  # the planted group, in each of three cycles
        assert result.skips == {"group already traded this run": 2}
        (outcome,) = result.outcomes
        # Decided in cycle 1, filled on cycle 2's book, nothing to repair on cycle 3's.
        assert outcome.status == "open" and outcome.hedge == ()
        a = outcome.attribution
        assert (a.planned.raw, a.after_entry.raw, a.after_hedge.raw) == (7_480_000,) * 3
        assert store.paper_trades(paper_id=result.paper_id)[0].cycle_no == 1

    # Two of three mutually exclusive markets resolving YES is impossible under the model: the
    # position guaranteed $100 but is paid $50.
    exchange = PlantedExchange(
        results={A: ("finalized", "yes"), B: ("finalized", "yes"), C: ("finalized", "no")}
    )
    client, _ = make_client(exchange)
    with RecordStore(path) as store:
        async with client:
            summary = await settle_open_trades(store, client)
        assert summary.settled == 1 and len(summary.violations) == 1
        (trade,) = store.paper_trades()
        assert (trade.payout, trade.realized_pnl, trade.model_violation) == (
            50_000_000,
            -42_520_000,
            True,
        )


async def test_replay_misses_a_trade_the_recording_ends_before(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await record_scan(path, confirmations=1)
    with RecordStore(path) as store:
        recorded = detect_config_from_json(store.run(run_id).config["detect"])
        (outcome,) = simulate_paper(store, run_id, recorded, PaperConfig()).outcomes
        assert outcome.status == "missed"
        (trade,) = store.paper_trades()
        assert (trade.status, trade.realized_pnl) == ("missed", 0)
