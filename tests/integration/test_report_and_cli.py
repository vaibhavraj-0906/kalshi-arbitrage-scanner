"""The HTML report and the research CLI, run against a recording of the mock exchange."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

from karb import cli
from karb.cli import app
from karb.dashboard import _hbars, _table, collect_report, render_report
from karb.exchange.client import KalshiClient
from karb.store.database import RecordStore
from karb.trading.auth import KEY_FILE_ENV, KEY_ID_ENV
from tests.integration.planted import (
    BASE,
    CREDENTIALS,
    HOST,
    PLANTED,
    PlantedExchange,
    trade_live,
)
from tests.support import captured_at

runner = CliRunner()
NO_KEYS = {KEY_ID_ENV: "", KEY_FILE_ENV: "", "COLUMNS": "200"}


def plain(text: str) -> str:
    """Typer forces colour under GitHub Actions; strip the escape codes."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


async def recorded_with_a_trade(path: Path) -> str:
    trader = await trade_live(path, PlantedExchange(), confirmations=3)
    return trader.run_id


async def test_report_renders_every_section(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    run_id = await recorded_with_a_trade(path)
    with RecordStore(path, read_only=True) as store:
        data = collect_report(store, run_id, generated_at=captured_at())
    page = render_report(data)

    assert data.stats.observations == 6  # two groups, three cycles
    # The trade took the overround's size in cycle 1, so later cycles never saw it again.
    assert data.solver_positive == 1 and data.stats.verified_observations == 1
    assert [row.verified_observations for row in data.sensitivity or []] == [1] * 5
    assert len(data.trades) == 1 and data.sessions[0].kind == "simulated"
    for heading in (
        "Where the edge goes",
        "Fee sensitivity",
        "How long opportunities lasted",
        "Trading: where the planned edge went",
        "<h2>Trades</h2>",
        "Screen hits",
        "Method and caveats",
    ):
        assert heading in page
    assert "No trade has settled yet" in page  # the trade is still open
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
    run_id = await recorded_with_a_trade(path)
    db = ["--db", str(path)]

    runs = runner.invoke(app, ["runs", *db], env={"COLUMNS": "200"})
    assert runs.exit_code == 0 and run_id in runs.stdout and "taker 0.07" in runs.stdout

    replay = runner.invoke(app, ["replay", *db])
    assert replay.exit_code == 0 and "identical to the live run" in replay.stdout

    pnl = runner.invoke(app, ["pnl", *db], env={"COLUMNS": "200"})
    assert pnl.exit_code == 0 and "OVERROUND LOGICAL" in pnl.stdout and "$7.48" in pnl.stdout
    assert "simulated exchange" in pnl.stdout

    out = tmp_path / "report.html"
    report = runner.invoke(app, ["report", *db, "--out", str(out), "--no-sensitivity"])
    assert report.exit_code == 0 and out.exists()
    assert "Fee sensitivity" not in out.read_text(encoding="utf-8")

    missing = runner.invoke(app, ["stats", "--db", str(tmp_path / "nope.duckdb")])
    assert missing.exit_code == 1


def test_settle_leaves_simulated_sessions_to_the_demo(tmp_path: Path) -> None:
    path = tmp_path / "karb.duckdb"
    asyncio.run(recorded_with_a_trade(path))
    settle = runner.invoke(app, ["settle", "--db", str(path)], env=NO_KEYS)
    assert settle.exit_code == 0 and "karb demo settles it" in plain(settle.stdout)


def test_trading_commands_need_demo_keys(tmp_path: Path) -> None:
    for command in (
        ["account"],
        ["trade", "--once", "--db", str(tmp_path / "t.duckdb")],
        ["order", "KXTEST-1", "--side", "yes", "--limit", "0.05", "--yes"],
    ):
        result = runner.invoke(app, command, env=NO_KEYS)
        assert result.exit_code == 1, command
        assert KEY_ID_ENV in plain(result.output) and KEY_FILE_ENV in plain(result.output)
    assert not (tmp_path / "t.duckdb").exists()  # nothing recorded without keys


def test_paper_trading_is_gone() -> None:
    result = runner.invoke(app, ["scan", "--paper", "--once"], env={"COLUMNS": "200"})
    assert result.exit_code != 0 and "No such option" in plain(result.output)
    assert runner.invoke(app, ["paper-replay"]).exit_code != 0


def test_demo_trading_through_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``account``, ``order`` and ``trade`` wired end to end, with a real key file, against the
    simulated desk standing in for Kalshi's demo exchange."""
    exchange = PlantedExchange()
    key_file = tmp_path / "demo.pem"
    key_file.write_bytes(
        CREDENTIALS._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    env = {KEY_ID_ENV: CREDENTIALS.key_id, KEY_FILE_ENV: str(key_file), "COLUMNS": "200"}
    real_client = cli.KalshiClient

    async def sleep(seconds: float) -> None:
        exchange.clock.advance(seconds)
        await asyncio.sleep(0)

    def client(**options: Any) -> KalshiClient:
        options.update(
            base_url=BASE,
            transport=httpx.MockTransport(exchange),
            clock=exchange.clock,
            sleep=sleep,
            sign_hosts=frozenset({HOST}),
        )
        return real_client(**options)

    monkeypatch.setattr(cli, "KalshiClient", client)
    db = str(tmp_path / "trading.duckdb")

    account = runner.invoke(app, ["account"], env=env)
    assert account.exit_code == 0, account.output
    assert "balance $10000.00" in plain(account.stdout) and "No open positions" in account.stdout

    order = runner.invoke(
        app, ["order", PLANTED[0], "--side", "yes", "--limit", "0.45", "--yes"], env=env
    )
    assert order.exit_code == 0, order.output
    # One YES at 0.45: fee ceil(0.07 x 0.45 x 0.55) = $0.017325, the model's to the micro-dollar.
    assert "fill 1.00 at 0.4500, fee $0.017325" in plain(order.stdout)
    assert "within the model's" in plain(order.stdout)
    exchange.desk.positions.clear()  # forget it, so the trades below start flat

    exercise = runner.invoke(app, ["trade", "--exercise", "PLANT-1", "--yes", "--db", db], env=env)
    assert exercise.exit_code == 0, exercise.output
    assert "EXERCISE" in plain(exercise.stdout) and "netted back $1.00" in plain(exercise.stdout)

    scan = runner.invoke(app, ["trade", "--once", "--db", db], env=env)
    assert scan.exit_code == 0, scan.output
    assert "trading on Kalshi's demo exchange" in plain(scan.output)
    with RecordStore(db, read_only=True) as store:
        kinds = sorted(trade.kind for trade in store.trades())
    assert "EXERCISE" in kinds
