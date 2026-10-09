"""``karb demo`` is the guide's offline tour: these are the numbers docs/guide.md promises."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from karb.cli import app
from karb.core.fixed import Cash
from karb.demo import run_demo
from karb.store.codec import detect_config_from_json
from karb.store.database import LIVE_SOURCE, RecordStore
from karb.store.replay import compare_with_live, replay_run
from karb.store.stats import fee_sensitivity


async def test_demo_reproduces_the_guide(tmp_path: Path) -> None:
    path = tmp_path / "demo.duckdb"
    summary = await run_demo(path)
    assert (summary.cycles, summary.trades, summary.settlement.settled) == (3, 2, 2)
    assert summary.realized == Cash.parse("-6.46")

    with RecordStore(path) as store:
        trades = {trade.group_key: trade for trade in store.paper_trades()}
        ladder, winner = trades["DEMO-LADDER"], trades["DEMO-WINNER"]
        assert (ladder.kind, ladder.planned_pnl, ladder.realized_pnl) == (
            "MONOTONE",
            1_960_000,
            1_960_000,
        )
        assert (winner.kind, winner.planned_pnl, winner.worst_after_entry) == (
            "OVERROUND",
            7_480_000,
            -11_680_000,
        )
        assert (winner.worst_after_hedge, winner.realized_pnl) == (-8_420_000, -8_420_000)
        assert not any(trade.model_violation for trade in trades.values())

        opportunities = store.opportunities(LIVE_SOURCE, summary.run_id)
        assert {row.group_key for row in opportunities} == {
            "DEMO-WINNER",
            "DEMO-LADDER",
        }  # not RANGE

        recorded = detect_config_from_json(store.run(summary.run_id).config["detect"])
        assert compare_with_live(store, replay_run(store, summary.run_id, recorded)).identical
        totals = [row.best_total for row in fee_sensitivity(store, summary.run_id, recorded)]
        # Direct members align to $0.0001, so each fill rounds by less: $9.451, shown as $9.45.
        assert totals == [9_440_000, 13_000_000, 11_210_000, 9_451_000, 9_440_000]


def test_demo_command_refuses_to_clobber(tmp_path: Path) -> None:
    runner = CliRunner()
    db = str(tmp_path / "demo.duckdb")
    first = runner.invoke(app, ["demo", "--db", db])
    assert first.exit_code == 0 and "realized -$6.46" in first.stdout
    assert runner.invoke(app, ["demo", "--db", db]).exit_code == 1
    assert runner.invoke(app, ["demo", "--db", db, "--overwrite"]).exit_code == 0

    stranger = tmp_path / "notes.duckdb"
    stranger.write_bytes(b"not a database")
    assert runner.invoke(app, ["demo", "--db", str(stranger), "--overwrite"]).exit_code == 1
    assert stranger.read_bytes() == b"not a database"
