"""The recording database: exact bulk writes, and upgrades from earlier schemas."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from karb.core.fixed import Price, Qty
from karb.market.book import Level, OrderBook
from karb.store.database import (
    SCHEMA_VERSION,
    RecordStore,
    SessionRow,
    SettlementRow,
    StoreError,
)


def test_bulk_writes_round_trip_extreme_integers_and_doubles(tmp_path: Path) -> None:
    huge = Qty(2**62)
    odd = Qty(2**53 + 1)  # not representable as a float: must not pass through one
    book = OrderBook(
        "T", (Level(Price(9_999), huge), Level(Price(1), odd)), (Level(Price(5_000), Qty(1)),)
    )
    with RecordStore(tmp_path / "x.duckdb") as store:
        store.insert_books("run", 1, {"T": book, "EMPTY": OrderBook("EMPTY", (), ())})
        store.insert_solver_results("live", "run", 1, [("G", "LOGICAL", "optimal", 0.1 + 0.2)])
        assert store.books("run", 1) == {"T": book, "EMPTY": OrderBook("EMPTY", (), ())}
        # 0.1 + 0.2 is 0.30000000000000004: strictly above 0.3, and equal to itself, only if
        # the double survived the JSON round trip bit for bit.
        assert store.solver_positive("live", "run", 0.3) == {"LOGICAL": 1}
        assert store.solver_positive("live", "run", 0.1 + 0.2) == {}


def test_settlements_upsert_and_select(tmp_path: Path) -> None:
    with RecordStore(tmp_path / "x.duckdb") as store:
        store.upsert_settlements([SettlementRow("A", "determined", "yes", None, None, 1)])
        store.upsert_settlements([SettlementRow("A", "finalized", "yes", 10_000, 5, 2)])
        assert store.settlements(["A", "B"]) == {
            "A": SettlementRow("A", "finalized", "yes", 10_000, 5, 2)
        }


def test_a_version_1_recording_is_upgraded_in_place(tmp_path: Path) -> None:
    path = tmp_path / "v1.duckdb"
    with RecordStore(path) as store:
        store.insert_run("r1", 1, "0.1.0", {"detect": {}})
    connection = duckdb.connect(str(path))
    for table in ("trade_sessions", "trades", "trade_orders", "settlements"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    connection.close()

    with RecordStore(path, read_only=True) as old:
        assert old.schema_version == 1
        assert old.trades() == [] and old.sessions() == []
        assert [run.run_id for run in old.runs()] == ["r1"]

    with RecordStore(path) as upgraded:
        assert upgraded.schema_version == SCHEMA_VERSION
        upgraded.insert_session(SessionRow("s1", "r1", "demo", 2, "0.1.0", {}))
        assert [s.session_id for s in upgraded.sessions()] == ["s1"]


_V2_PAPER_TABLES = """
CREATE TABLE paper_sessions (
    paper_id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL, kind VARCHAR NOT NULL,
    created_ns BIGINT NOT NULL, karb_version VARCHAR NOT NULL, config VARCHAR NOT NULL
);
CREATE TABLE paper_trades (
    trade_id VARCHAR PRIMARY KEY, paper_id VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
    cycle_no INTEGER NOT NULL, group_key VARCHAR NOT NULL, event_ticker VARCHAR NOT NULL,
    opportunity_id VARCHAR NOT NULL, kind VARCHAR NOT NULL, tier VARCHAR NOT NULL,
    decided_ns BIGINT NOT NULL, entry_ns BIGINT, hedge_ns BIGINT, planned_cost BIGINT NOT NULL,
    planned_pnl BIGINT NOT NULL, entry_cost BIGINT NOT NULL, worst_after_entry BIGINT NOT NULL,
    hedge_cost BIGINT NOT NULL, worst_after_hedge BIGINT NOT NULL,
    best_after_hedge BIGINT NOT NULL, planned_contracts BIGINT NOT NULL,
    filled_contracts BIGINT NOT NULL, status VARCHAR NOT NULL, note VARCHAR NOT NULL,
    settled_ns BIGINT, payout BIGINT, realized_pnl BIGINT, model_violation BOOLEAN
);
CREATE TABLE paper_orders (
    trade_id VARCHAR NOT NULL, phase VARCHAR NOT NULL, seq INTEGER NOT NULL,
    ticker VARCHAR NOT NULL, side VARCHAR NOT NULL, limit_price INTEGER NOT NULL,
    ordered BIGINT NOT NULL, filled BIGINT NOT NULL, cash_out BIGINT NOT NULL,
    fees BIGINT NOT NULL, fills VARCHAR NOT NULL
);
INSERT INTO paper_sessions VALUES ('p1', 'r1', 'live', 1, '0.1.0', '{"paper": {"hedge": true}}');
INSERT INTO paper_trades VALUES ('p1-0001', 'p1', 'r1', 1, 'G', 'EV', 'opp', 'OVERROUND',
    'LOGICAL', 10, 11, NULL, 92520000, 7480000, 92520000, 7480000, 0, 7480000, 57480000,
    15000, 15000, 'open', '', NULL, NULL, NULL, NULL);
INSERT INTO paper_orders VALUES ('p1-0001', 'entry', 0, 'A', 'no', 6000, 5000, 5000,
    30840000, 840000, '[]');
"""


def write_v2(path: Path) -> None:
    """A recording as karb 0.1 left it: paper tables, schema v2."""
    with RecordStore(path) as store:
        store.insert_run("r1", 1, "0.1.0", {"detect": {}})
    connection = duckdb.connect(str(path))
    for table in ("trade_sessions", "trades", "trade_orders"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute(_V2_PAPER_TABLES)
    connection.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    connection.close()


def test_a_version_2_recording_reads_as_is_and_migrates_on_write(tmp_path: Path) -> None:
    path = tmp_path / "v2.duckdb"
    write_v2(path)

    with RecordStore(path, read_only=True) as old:  # read-only: nothing on disk changes
        assert old.schema_version == 2
        (session,) = old.sessions()
        assert (session.session_id, session.kind) == ("p1", "live")
        (trade,) = old.trades()
        assert (trade.session_id, trade.planned_pnl, trade.netted_cash) == ("p1", 7_480_000, None)
        (order,) = old.trade_orders("p1-0001")
        assert (order.ticker, order.fees, order.client_order_id) == ("A", 840_000, None)

    with RecordStore(path) as upgraded:
        assert upgraded.schema_version == SCHEMA_VERSION == 3
        assert [t.trade_id for t in upgraded.trades(session_id="p1")] == ["p1-0001"]
        assert upgraded.table_counts()["trade_orders"] == 1
    tables = {row[0] for row in duckdb.connect(str(path)).execute("SHOW TABLES").fetchall()}
    assert "paper_trades" not in tables and "trades" in tables


def test_newer_schemas_and_strangers_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.duckdb"
    with RecordStore(path):
        pass
    connection = duckdb.connect(str(path))
    connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    connection.close()
    with pytest.raises(StoreError, match="v99"):
        RecordStore(path, read_only=True)

    stranger = tmp_path / "other.duckdb"
    duckdb.connect(str(stranger)).close()
    with pytest.raises(StoreError, match="not a karb recording"):
        RecordStore(stranger, read_only=True)
