"""Trading end to end: scanner, signed orders, the simulated desk, settlement, and audits."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from karb.arb.detect import DetectConfig
from karb.core.fixed import Cash
from karb.market.book import Side
from karb.scanner.service import CycleReport
from karb.store.database import RecordStore
from karb.trading.engine import Trader
from karb.trading.exercise import complete_set, fetch_event_snapshots
from karb.trading.plan import TradeConfig, TradePlan
from karb.trading.settle import settle_open_trades
from tests.integration.planted import PLANTED, PlantedExchange, make_client, trade_live
from tests.unit.test_engine import bracket_snapshot

A, B, C = PLANTED
C_WINS = {A: ("finalized", "no"), B: ("finalized", "no"), C: ("finalized", "yes")}


async def test_a_half_filled_basket_is_repaired_settled_and_reconciled(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    exchange = PlantedExchange(thin_on_arrival=frozenset({C}), results=C_WINS)
    trader = await trade_live(path, exchange)

    assert trader.errors == [] and trader.halted is None
    desk = exchange.desk
    # Three entry orders in one batch, then two repairs; every request was signed and verified.
    assert [o["ticker"] for o in desk.orders] == [A, B, C, A, B]
    assert ("POST", "/portfolio/events/orders/batched") in desk.requests
    with RecordStore(path, read_only=True) as store:
        (trade,) = store.trades()
        # Priced by hand in tests/unit/test_trading_plan.py.
        assert (trade.planned_pnl, trade.worst_after_entry, trade.worst_after_hedge) == (
            7_480_000,
            -11_680_000,
            -8_420_000,
        )
        # The YES repairs closed the NO positions: $100 back at once, and a balance that moved
        # by exactly the guaranteed result.
        assert (trade.netted_cash, trade.balance_change) == (100_000_000, -8_420_000)
        assert desk.positions == {}
        orders = [o for o in store.trade_orders(trade.trade_id) if o.phase != "plan"]
        assert all(o.fees == o.model_fees for o in orders)
        assert all(o.order_id and o.client_order_id for o in orders)
        assert "AUDIT" not in trade.note

    exchange.desk.settle_all()
    client, _ = make_client(exchange, signed=True)
    with RecordStore(path) as store:
        async with client:
            summary = await settle_open_trades(store, client)
        assert (summary.settled, summary.pending, summary.violations) == (1, 0, [])
        # The exchange paid nothing at settlement -- it had netted everything -- and the model
        # agrees once the $100 returned at execution is counted.
        assert (summary.exchange_checked, summary.exchange_mismatches) == (1, [])
        (settled,) = store.trades()
        assert (settled.payout, settled.realized_pnl) == (100_000_000, -8_420_000)
        assert set(store.settlements(list(PLANTED))) == {A, B}


async def test_a_full_fill_settles_against_the_exchange_record(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    # Two of three mutually exclusive markets resolving YES is impossible under the model: the
    # position guaranteed $100 but is paid $50.
    exchange = PlantedExchange(
        results={A: ("finalized", "yes"), B: ("finalized", "yes"), C: ("finalized", "no")}
    )
    trader = await trade_live(path, exchange)
    # The orders took the size behind the overround, so cycle 2 no longer saw it.
    assert trader.skips == {} and len(trader.outcomes) == 1
    assert exchange.desk.positions == {A: -5000, B: -5000, C: -5000}
    exchange.desk.settle_all()
    client, _ = make_client(exchange, signed=True)
    with RecordStore(path) as store:
        async with client:
            summary = await settle_open_trades(store, client)
        assert summary.settled == 1 and len(summary.violations) == 1
        assert summary.exchange_mismatches == []  # the exchange paid the same $50
        (trade,) = store.trades()
        assert (trade.payout, trade.realized_pnl, trade.model_violation) == (
            50_000_000,
            -42_520_000,
            True,
        )


async def test_settlement_waits_for_finalized_results(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    exchange = PlantedExchange(
        thin_on_arrival=frozenset({C}),
        results={**C_WINS, A: ("determined", "no")},  # A is held; C is not
    )
    await trade_live(path, exchange)
    client, _ = make_client(exchange, signed=True)
    with RecordStore(path) as store:
        async with client:
            summary = await settle_open_trades(store, client)
        assert (summary.settled, summary.pending) == (0, 1)
        assert store.trades()[0].status == "open"


async def test_a_lost_response_does_not_place_the_orders_twice(tmp_path: Path) -> None:
    exchange = PlantedExchange()
    exchange.desk.lose_responses = 1  # the batch is accepted, then the connection resets
    trader = await trade_live(tmp_path / "karb.duckdb", exchange)
    # The retry carried the same client order ids, so the desk refused the copies, and karb
    # found the originals by id.
    assert len(exchange.desk.orders) == 3
    (outcome,) = trader.outcomes
    assert outcome.filled_contracts == outcome.planned_contracts
    assert all(p.error == "" for p in outcome.entry)
    assert trader.halted is None


async def test_fees_above_the_model_halt_trading(tmp_path: Path) -> None:
    exchange = PlantedExchange()
    exchange.desk.fee_scale = Fraction(2)
    path = tmp_path / "karb.duckdb"
    reports: list[CycleReport] = []
    trader = await trade_live(path, exchange, reports=reports)
    assert trader.halted is not None and "fee model allowed" in trader.halted
    assert len(trader.outcomes) == 1  # the trade that revealed it is recorded
    with RecordStore(path, read_only=True) as store:
        assert "AUDIT:" in store.trades()[0].note
    # Nothing more is traded this session, even an opportunity never seen before.
    trader.consider(reports[0], 1)
    assert trader.skips["trading halted"] == 1


async def test_existing_positions_are_left_alone(tmp_path: Path) -> None:
    exchange = PlantedExchange()
    exchange.desk.positions[B] = 700  # 7 YES on B, bought some other way
    trader = await trade_live(tmp_path / "karb.duckdb", exchange)
    assert trader.outcomes == []
    assert trader.skips["account already holds a position in the basket's markets"] == 1
    assert exchange.desk.orders == []


async def test_a_stop_file_halts_trading_but_not_the_scan(tmp_path: Path) -> None:
    stop = tmp_path / "STOP"
    stop.write_text("")
    exchange = PlantedExchange()
    trader = await trade_live(tmp_path / "karb.duckdb", exchange, stop_file=stop)
    assert trader.halted is not None and "stop file" in trader.halted
    assert exchange.desk.orders == []


async def test_budget_and_balance_limits(tmp_path: Path) -> None:
    exchange = PlantedExchange()
    exchange.desk.balance = Cash.parse("50").raw  # less than the $92.52 basket
    trader = await trade_live(tmp_path / "a.duckdb", exchange)
    assert trader.skips["balance below the basket's cost"] == 1
    assert exchange.desk.orders == []

    capped = await trade_live(
        tmp_path / "b.duckdb",
        PlantedExchange(),
        config=TradeConfig(capital=Cash.parse("50")),
    )
    assert capped.skips["capital limit"] >= 1 and capped.outcomes == []


async def test_ctrl_c_drops_trades_not_yet_sent(tmp_path: Path) -> None:
    exchange = PlantedExchange()
    client, _ = make_client(exchange, signed=True)
    snapshot = bracket_snapshot()
    plan = complete_set(snapshot, sets=1, fee_config=DetectConfig().fees)
    assert isinstance(plan, TradePlan)
    async with client:
        with RecordStore(tmp_path / "t.duckdb") as store:
            trader = Trader(
                client,
                store,
                run_id="r",
                detect_config=DetectConfig(),
                config=TradeConfig(),
                environment="simulated",
            )
            assert trader.submit(plan, snapshot, 1)
            assert trader.submit(replace(plan, group_key="OTHER"), snapshot, 1)
            assert trader.pending == 2
            assert trader.cancel_queued() == 2
            await trader.drain()
    assert exchange.desk.orders == [] and trader.capital_in_use == Cash.ZERO
    assert trader.skips["scan stopped before the trade was sent"] == 2


async def test_an_exercise_trade_nets_and_reconciles(tmp_path: Path) -> None:
    """The planted event lists no exhaustive set, so the exercise buys YES and NO on one market.

    YES at 0.45 ($0.45 + fee $0.017325 -> $0.47) and NO at 0.60 ($0.60 + $0.0168 -> $0.62): $1.09
    for a pair that pays $1 whatever happens. The exchange nets it at once and returns the $1.
    """
    exchange = PlantedExchange()
    client, _ = make_client(exchange, signed=True)
    async with client:
        snapshots, _ = await fetch_event_snapshots(client, "PLANT-1", max_levels=10)
        (snapshot,) = snapshots
        plan = complete_set(snapshot, sets=1, fee_config=DetectConfig().fees)
        assert isinstance(plan, TradePlan), plan
        assert {(o.ticker, o.side) for o in plan.orders} == {(A, Side.YES), (A, Side.NO)}
        assert plan.planned_cost == Cash.parse("1.09")
        with RecordStore(tmp_path / "t.duckdb") as store:
            trader = Trader(
                client,
                store,
                run_id="exercise",
                detect_config=DetectConfig(),
                config=TradeConfig(),
                environment="simulated",
            )
            outcome = await trader.execute(plan, snapshot)
            (trade,) = store.trades()
    assert outcome is not None and trader.halted is None
    assert outcome.attribution.after_hedge == Cash.parse("-0.09")
    assert (trade.kind, trade.netted_cash, trade.balance_change) == ("EXERCISE", 1_000_000, -90_000)
    assert exchange.desk.positions == {}


async def test_an_unreadable_account_after_the_orders_is_recorded_and_halts(
    tmp_path: Path,
) -> None:
    exchange = PlantedExchange()
    exchange.desk.account_unreadable_after_orders = True
    path = tmp_path / "karb.duckdb"
    trader = await trade_live(path, exchange)
    assert trader.halted is not None and "could not read the account" in trader.halted
    with RecordStore(path, read_only=True) as store:
        (trade,) = store.trades()  # the orders went out, so the trade is on record
        assert trade.balance_change is None and "AUDIT:" in trade.note
        assert trade.filled_contracts == trade.planned_contracts
