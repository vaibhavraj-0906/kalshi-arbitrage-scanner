"""The HTML report and the research CLI, run against a recording of the mock exchange."""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from karb.cli import app
from karb.dashboard import _hbars, _table, collect_report, render_report
from karb.paper.replay import simulate_paper
from karb.paper.trade import PaperConfig
from karb.store.codec import detect_config_from_json
from karb.store.database import RecordStore
from tests.integration.planted import record_scan
from tests.support import captured_at

runner = CliRunner()


async def recorded_with_paper(path: Path) -> str:
    run_id = await record_scan(path, confirmations=3)
    with RecordStore(path) as store:
        recorded = detect_config_from_json(store.run(run_id).config["detect"])
        simulate_paper(store, run_id, recorded, PaperConfig(), now=captured_at())
    return run_id


async def test_report_renders_every_section(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await recorded_with_paper(path)
    with RecordStore(path, read_only=True) as store:
        data = collect_report(store, run_id, generated_at=captured_at())
    page = render_report(data)

    assert data.stats.observations == 6  # two groups, three cycles
    assert data.solver_positive == 3 and data.stats.verified_observations == 3
    assert [row.verified_observations for row in data.sensitivity or []] == [3] * 5
    for heading in (
        "Where the edge goes",
        "Fee sensitivity",
        "How long opportunities lasted",
        "Paper trading: where the planned edge went",
        "Paper trades",
        "Screen hits",
        "Method and caveats",
    ):
        assert heading in page
    assert "No paper trade has settled yet" in page  # the replayed trade is still open
    assert page.count("<details>") == 3  # a table view under every chart
    assert "taker fee 0.07" in page
    assert not re.search(r"https?://", page.split("<script>")[0].split("<style>")[0])


def test_labels_from_the_exchange_are_escaped() -> None:
    hostile = '<img src=x onerror="alert(1)">'
    html = _hbars([(hostile, 1, "1", "series-1")], aria="t") + _table(["a"], [(hostile,)])
    assert "<img" not in html
    assert "&lt;img" in html and "&quot;" in html


async def test_research_commands(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await recorded_with_paper(path)
    db = ["--db", str(path)]

    runs = runner.invoke(app, ["runs", *db], env={"COLUMNS": "200"})
    assert runs.exit_code == 0 and run_id in runs.stdout and "taker 0.07" in runs.stdout

    replay = runner.invoke(app, ["replay", *db])
    assert replay.exit_code == 0 and "identical to the live run" in replay.stdout

    pnl = runner.invoke(app, ["pnl", *db], env={"COLUMNS": "200"})
    assert pnl.exit_code == 0 and "OVERROUND LOGICAL" in pnl.stdout and "$7.48" in pnl.stdout

    paper = runner.invoke(app, ["paper-replay", *db, "--no-hedge"], env={"COLUMNS": "200"})
    assert paper.exit_code == 0 and "1 trades (1 open" in paper.stdout

    out = tmp_path / "report.html"
    report = runner.invoke(app, ["report", *db, "--out", str(out), "--no-sensitivity"])
    assert report.exit_code == 0 and out.exists()
    assert "Fee sensitivity" not in out.read_text(encoding="utf-8")

    missing = runner.invoke(app, ["stats", "--db", str(tmp_path / "nope.duckdb")])
    assert missing.exit_code == 1


def test_paper_needs_a_recording() -> None:
    result = runner.invoke(app, ["scan", "--paper", "--once"], env={"COLUMNS": "200"})
    assert result.exit_code != 0
    # Typer forces colour under GitHub Actions, so strip the escape codes first.
    assert "--paper needs --record" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)
