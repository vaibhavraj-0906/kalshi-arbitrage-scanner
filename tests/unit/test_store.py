"""The recording database: exact bulk writes, and upgrades from earlier schemas."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from karb.core.fixed import Price, Qty
from karb.market.book import Level, OrderBook
from karb.store.database import (
    SCHEMA_VERSION,
    PaperSessionRow,
    RecordStore,
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
    for table in ("paper_sessions", "paper_trades", "paper_orders", "settlements"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    connection.close()

    with RecordStore(path, read_only=True) as old:
        assert old.schema_version == 1
        assert old.paper_trades() == [] and old.paper_sessions() == []
        assert [run.run_id for run in old.runs()] == ["r1"]

    with RecordStore(path) as upgraded:
        assert upgraded.schema_version == SCHEMA_VERSION
        upgraded.insert_paper_session(PaperSessionRow("p1", "r1", "replay", 2, "0.1.0", {}))
        assert [s.paper_id for s in upgraded.paper_sessions()] == ["p1"]


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
