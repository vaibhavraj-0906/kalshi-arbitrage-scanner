"""The recording database: one DuckDB file, append-only (docs/decisions/ADR-0007).

Conventions follow tengine's market-data store. Money is exact integers -- prices in $0.0001,
quantities in 0.01 contracts, cash in $0.000001 -- never ``DOUBLE``. Timestamps are integer
nanoseconds since the epoch. The ``DOUBLE`` columns are diagnostics, never money: the LP's
pre-rounding objective and a paper trade's fill ratio.

Rows are written in bulk as one JSON document unpacked by DuckDB's ``from_json``. Parameter
binding is the obvious alternative and is unusable here: on DuckDB 1.5, ``executemany`` took
45 seconds to write 188 order books (list columns bind at about a quarter-second per row), where
the JSON path takes 15 milliseconds and round-trips every integer exactly.

DuckDB lets one process write a file at a time. Stop a recording ``karb scan`` before reading the
same file with ``karb stats``, or read a copy.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import astuple, dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self

from karb.arb.opportunity import Opportunity
from karb.market.book import OrderBook
from karb.store.codec import book_from_row, book_to_row, legs_json, to_ns
from karb.wire.decode import load_json

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "LIVE_SOURCE",
    "SCHEMA_VERSION",
    "ObservationRow",
    "OpportunityRow",
    "PaperOrderRow",
    "PaperSessionRow",
    "PaperTradeRow",
    "RecordStore",
    "RunInfo",
    "SettlementRow",
    "StoreError",
    "new_id",
]

SCHEMA_VERSION: Final = 2
"""v1: recordings and replays (Milestone 2). v2 adds paper trading and settlements."""

LIVE_SOURCE: Final = "live"
"""The ``source`` of rows the scanner recorded itself. Saved replays use their replay id."""

_PAPER_TABLES: Final = ("paper_sessions", "paper_trades", "paper_orders", "settlements")
_TABLES: Final = (
    "runs",
    "cycles",
    "event_payloads",
    "observations",
    "books",
    "screen_hits",
    "solver_results",
    "opportunities",
    "replays",
    *_PAPER_TABLES,
)

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       VARCHAR PRIMARY KEY,
    started_ns   BIGINT  NOT NULL,
    finished_ns  BIGINT,
    karb_version VARCHAR NOT NULL,
    config       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    run_id       VARCHAR NOT NULL,
    cycle_no     INTEGER NOT NULL,
    started_ns   BIGINT  NOT NULL,
    finished_ns  BIGINT  NOT NULL,
    rescreened   BOOLEAN NOT NULL,
    screened     INTEGER NOT NULL,
    untradeable  INTEGER NOT NULL,
    targets      INTEGER NOT NULL,
    fetch_errors INTEGER NOT NULL,
    PRIMARY KEY (run_id, cycle_no)
);

CREATE TABLE IF NOT EXISTS event_payloads (
    payload_hash VARCHAR PRIMARY KEY,
    event_ticker VARCHAR NOT NULL,
    recorded_ns  BIGINT  NOT NULL,
    payload      VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    run_id                VARCHAR   NOT NULL,
    cycle_no              INTEGER   NOT NULL,
    group_key             VARCHAR   NOT NULL,
    event_ticker          VARCHAR   NOT NULL,
    series_ticker         VARCHAR   NOT NULL,
    payload_hash          VARCHAR   NOT NULL,
    series_fee_type       VARCHAR   NOT NULL,
    series_fee_multiplier VARCHAR   NOT NULL,
    observed_ns           BIGINT    NOT NULL,
    skew_ns               BIGINT    NOT NULL,
    tradeable             VARCHAR[] NOT NULL,
    PRIMARY KEY (run_id, cycle_no, group_key)
);

CREATE TABLE IF NOT EXISTS books (
    run_id     VARCHAR   NOT NULL,
    cycle_no   INTEGER   NOT NULL,
    ticker     VARCHAR   NOT NULL,
    yes_prices INTEGER[] NOT NULL,
    yes_qtys   BIGINT[]  NOT NULL,
    no_prices  INTEGER[] NOT NULL,
    no_qtys    BIGINT[]  NOT NULL
);

CREATE TABLE IF NOT EXISTS screen_hits (
    run_id     VARCHAR NOT NULL,
    cycle_no   INTEGER NOT NULL,
    group_key  VARCHAR NOT NULL,
    tier       VARCHAR NOT NULL,
    rule       VARCHAR NOT NULL,
    gross_edge INTEGER NOT NULL,
    detail     VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS solver_results (
    source    VARCHAR NOT NULL,
    run_id    VARCHAR NOT NULL,
    cycle_no  INTEGER NOT NULL,
    group_key VARCHAR NOT NULL,
    tier      VARCHAR NOT NULL,
    status    VARCHAR NOT NULL,
    profit    DOUBLE  NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    source         VARCHAR NOT NULL,
    run_id         VARCHAR NOT NULL,
    cycle_no       INTEGER NOT NULL,
    group_key      VARCHAR NOT NULL,
    opportunity_id VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL,
    tier           VARCHAR NOT NULL,
    observed_ns    BIGINT  NOT NULL,
    expires_ns     BIGINT,
    cost           BIGINT  NOT NULL,
    fees           BIGINT  NOT NULL,
    guaranteed_pnl BIGINT  NOT NULL,
    best_pnl       BIGINT  NOT NULL,
    contracts      BIGINT  NOT NULL,
    legs           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS replays (
    replay_id    VARCHAR PRIMARY KEY,
    run_id       VARCHAR NOT NULL,
    created_ns   BIGINT  NOT NULL,
    karb_version VARCHAR NOT NULL,
    config       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_sessions (
    paper_id     VARCHAR PRIMARY KEY,
    run_id       VARCHAR NOT NULL,
    kind         VARCHAR NOT NULL,
    created_ns   BIGINT  NOT NULL,
    karb_version VARCHAR NOT NULL,
    config       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id          VARCHAR PRIMARY KEY,
    paper_id          VARCHAR NOT NULL,
    run_id            VARCHAR NOT NULL,
    cycle_no          INTEGER NOT NULL,
    group_key         VARCHAR NOT NULL,
    event_ticker      VARCHAR NOT NULL,
    opportunity_id    VARCHAR NOT NULL,
    kind              VARCHAR NOT NULL,
    tier              VARCHAR NOT NULL,
    decided_ns        BIGINT  NOT NULL,
    entry_ns          BIGINT,
    hedge_ns          BIGINT,
    planned_cost      BIGINT  NOT NULL,
    planned_pnl       BIGINT  NOT NULL,
    entry_cost        BIGINT  NOT NULL,
    worst_after_entry BIGINT  NOT NULL,
    hedge_cost        BIGINT  NOT NULL,
    worst_after_hedge BIGINT  NOT NULL,
    best_after_hedge  BIGINT  NOT NULL,
    planned_contracts BIGINT  NOT NULL,
    filled_contracts  BIGINT  NOT NULL,
    status            VARCHAR NOT NULL,
    note              VARCHAR NOT NULL,
    settled_ns        BIGINT,
    payout            BIGINT,
    realized_pnl      BIGINT,
    model_violation   BOOLEAN
);

CREATE TABLE IF NOT EXISTS paper_orders (
    trade_id    VARCHAR NOT NULL,
    phase       VARCHAR NOT NULL,
    seq         INTEGER NOT NULL,
    ticker      VARCHAR NOT NULL,
    side        VARCHAR NOT NULL,
    limit_price INTEGER NOT NULL,
    ordered     BIGINT  NOT NULL,
    filled      BIGINT  NOT NULL,
    cash_out    BIGINT  NOT NULL,
    fees        BIGINT  NOT NULL,
    fills       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    ticker     VARCHAR PRIMARY KEY,
    status     VARCHAR NOT NULL,
    result     VARCHAR NOT NULL,
    yes_value  INTEGER,
    settled_ns BIGINT,
    fetched_ns BIGINT  NOT NULL
);
"""

_OBSERVATION_COLUMNS: Final = (
    ("run_id", "VARCHAR"),
    ("cycle_no", "INTEGER"),
    ("group_key", "VARCHAR"),
    ("event_ticker", "VARCHAR"),
    ("series_ticker", "VARCHAR"),
    ("payload_hash", "VARCHAR"),
    ("series_fee_type", "VARCHAR"),
    ("series_fee_multiplier", "VARCHAR"),
    ("observed_ns", "BIGINT"),
    ("skew_ns", "BIGINT"),
    ("tradeable", "VARCHAR[]"),
)
_BOOK_COLUMNS: Final = (
    ("run_id", "VARCHAR"),
    ("cycle_no", "INTEGER"),
    ("ticker", "VARCHAR"),
    ("yes_prices", "INTEGER[]"),
    ("yes_qtys", "BIGINT[]"),
    ("no_prices", "INTEGER[]"),
    ("no_qtys", "BIGINT[]"),
)
_SCREEN_HIT_COLUMNS: Final = (
    ("run_id", "VARCHAR"),
    ("cycle_no", "INTEGER"),
    ("group_key", "VARCHAR"),
    ("tier", "VARCHAR"),
    ("rule", "VARCHAR"),
    ("gross_edge", "INTEGER"),
    ("detail", "VARCHAR"),
)
_SOLVER_COLUMNS: Final = (
    ("source", "VARCHAR"),
    ("run_id", "VARCHAR"),
    ("cycle_no", "INTEGER"),
    ("group_key", "VARCHAR"),
    ("tier", "VARCHAR"),
    ("status", "VARCHAR"),
    ("profit", "DOUBLE"),
)
_OPPORTUNITY_COLUMNS: Final = (
    ("source", "VARCHAR"),
    ("run_id", "VARCHAR"),
    ("cycle_no", "INTEGER"),
    ("group_key", "VARCHAR"),
    ("opportunity_id", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("tier", "VARCHAR"),
    ("observed_ns", "BIGINT"),
    ("expires_ns", "BIGINT"),
    ("cost", "BIGINT"),
    ("fees", "BIGINT"),
    ("guaranteed_pnl", "BIGINT"),
    ("best_pnl", "BIGINT"),
    ("contracts", "BIGINT"),
    ("legs", "VARCHAR"),
)
_PAPER_TRADE_COLUMNS: Final = (
    ("trade_id", "VARCHAR"),
    ("paper_id", "VARCHAR"),
    ("run_id", "VARCHAR"),
    ("cycle_no", "INTEGER"),
    ("group_key", "VARCHAR"),
    ("event_ticker", "VARCHAR"),
    ("opportunity_id", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("tier", "VARCHAR"),
    ("decided_ns", "BIGINT"),
    ("entry_ns", "BIGINT"),
    ("hedge_ns", "BIGINT"),
    ("planned_cost", "BIGINT"),
    ("planned_pnl", "BIGINT"),
    ("entry_cost", "BIGINT"),
    ("worst_after_entry", "BIGINT"),
    ("hedge_cost", "BIGINT"),
    ("worst_after_hedge", "BIGINT"),
    ("best_after_hedge", "BIGINT"),
    ("planned_contracts", "BIGINT"),
    ("filled_contracts", "BIGINT"),
    ("status", "VARCHAR"),
    ("note", "VARCHAR"),
    ("settled_ns", "BIGINT"),
    ("payout", "BIGINT"),
    ("realized_pnl", "BIGINT"),
    ("model_violation", "BOOLEAN"),
)
_PAPER_ORDER_COLUMNS: Final = (
    ("trade_id", "VARCHAR"),
    ("phase", "VARCHAR"),
    ("seq", "INTEGER"),
    ("ticker", "VARCHAR"),
    ("side", "VARCHAR"),
    ("limit_price", "INTEGER"),
    ("ordered", "BIGINT"),
    ("filled", "BIGINT"),
    ("cash_out", "BIGINT"),
    ("fees", "BIGINT"),
    ("fills", "VARCHAR"),
)
_SETTLEMENT_COLUMNS: Final = (
    ("ticker", "VARCHAR"),
    ("status", "VARCHAR"),
    ("result", "VARCHAR"),
    ("yes_value", "INTEGER"),
    ("settled_ns", "BIGINT"),
    ("fetched_ns", "BIGINT"),
)


class StoreError(RuntimeError):
    """The file is not a karb recording, is from another schema, or lacks what was asked for."""


def new_id(now: datetime) -> str:
    """Sortable and unique, e.g. ``20260913T093000Z-a1b2c3``."""
    return f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True, slots=True)
class RunInfo:
    run_id: str
    started_ns: int
    finished_ns: int | None
    karb_version: str
    config: dict[str, Any]
    cycles: int
    observations: int
    live_opportunities: int


@dataclass(frozen=True, slots=True)
class ObservationRow:
    """One group's confirmation snapshot: everything needed to rebuild what detection saw."""

    run_id: str
    cycle_no: int
    group_key: str
    event_ticker: str
    series_ticker: str
    payload_hash: str
    series_fee_type: str
    series_fee_multiplier: str
    observed_ns: int
    skew_ns: int
    tradeable: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpportunityRow:
    source: str
    run_id: str
    cycle_no: int
    group_key: str
    opportunity_id: str
    kind: str
    tier: str
    observed_ns: int
    expires_ns: int | None
    cost: int
    fees: int
    guaranteed_pnl: int
    best_pnl: int
    contracts: int


@dataclass(frozen=True, slots=True)
class PaperSessionRow:
    paper_id: str
    run_id: str
    kind: str
    """``live`` (traded while scanning) or ``replay`` (simulated over a recording)."""
    created_ns: int
    karb_version: str
    config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PaperTradeRow:
    """One paper trade and its P&L attribution, in raw exact units (ADR-0008)."""

    trade_id: str
    paper_id: str
    run_id: str
    cycle_no: int
    group_key: str
    event_ticker: str
    opportunity_id: str
    kind: str
    tier: str
    decided_ns: int
    entry_ns: int | None
    hedge_ns: int | None
    planned_cost: int
    planned_pnl: int
    entry_cost: int
    worst_after_entry: int
    hedge_cost: int
    worst_after_hedge: int
    best_after_hedge: int
    planned_contracts: int
    filled_contracts: int
    status: str
    """``open`` (holding to settlement), ``flat`` (nothing filled), ``missed`` (no arrival
    book), or ``settled``."""
    note: str
    settled_ns: int | None = None
    payout: int | None = None
    realized_pnl: int | None = None
    model_violation: bool | None = None

    @property
    def total_cost(self) -> int:
        return self.entry_cost + self.hedge_cost


@dataclass(frozen=True, slots=True)
class PaperOrderRow:
    trade_id: str
    phase: str
    """``plan`` (the basket as decided), ``entry`` or ``hedge``."""
    seq: int
    ticker: str
    side: str
    limit_price: int
    ordered: int
    filled: int
    cash_out: int
    fees: int
    fills: str
    """Exact fills as JSON: price, qty, trade fee, rounding fee and rebate per level."""


@dataclass(frozen=True, slots=True)
class SettlementRow:
    ticker: str
    status: str
    result: str
    yes_value: int | None
    """What one YES contract paid, in $0.0001. ``None`` until the market is final."""
    settled_ns: int | None
    fetched_ns: int


class RecordStore:
    """A DuckDB file of recorded runs and paper trades."""

    __slots__ = ("_connection", "path", "read_only", "schema_version")

    def __init__(self, path: Path | str = ":memory:", *, read_only: bool = False) -> None:
        import duckdb

        self.path = str(path)
        self.read_only = read_only
        self.schema_version = SCHEMA_VERSION
        in_memory = self.path == ":memory:"
        if read_only and (in_memory or not Path(self.path).exists()):
            raise StoreError(f"no recording at {self.path}")
        if not in_memory and not read_only:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = duckdb.connect(self.path, read_only=read_only)
        except duckdb.Error as exc:
            raise StoreError(
                f"cannot open {self.path}: {exc} (is a recording still writing to it?)"
            ) from exc
        self._connection: duckdb.DuckDBPyConnection = connection
        try:
            if not read_only:
                self._connection.execute(_SCHEMA)
            self._check_schema()
        except BaseException:
            self._connection.close()
            raise

    def _check_schema(self) -> None:
        import duckdb

        try:
            row = self._connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except duckdb.Error as exc:
            raise StoreError(f"{self.path} is not a karb recording") from exc
        if row is None:
            if self.read_only:
                raise StoreError(f"{self.path} is not a karb recording")
            self._connection.execute(
                "INSERT INTO meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)]
            )
            return
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} holds schema v{version}; this karb reads up to v{SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION and not self.read_only:
            # Every version so far only adds tables, which _SCHEMA has just created.
            self._connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", [str(SCHEMA_VERSION)]
            )
            version = SCHEMA_VERSION
        self.schema_version = version

    @property
    def has_paper_tables(self) -> bool:
        return self.schema_version >= 2

    # ---- lifecycle ------------------------------------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Everything inside commits together, or not at all."""
        self._connection.begin()
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        self._connection.commit()

    def _insert_rows(
        self,
        table: str,
        columns: Sequence[tuple[str, str]],
        rows: Sequence[Sequence[object]],
        *,
        replace: bool = False,
    ) -> None:
        """Insert many rows in one statement: a JSON array of objects, unpacked by DuckDB.

        ``columns`` are fixed per table in this module, so building the statement from them is
        safe; values travel only inside the bound JSON document.
        """
        if not rows:
            return
        names = [name for name, _ in columns]
        structure = json.dumps([{name: kind for name, kind in columns}])
        document = json.dumps([dict(zip(names, row, strict=True)) for row in rows], allow_nan=False)
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        fields = ", ".join(f"r.{name}" for name in names)
        self._connection.execute(
            f"{verb} INTO {table} ({', '.join(names)}) SELECT {fields} FROM "
            f"(SELECT UNNEST(from_json(?::JSON, '{structure}')) AS r)",
            [document],
        )

    # ---- writing: recordings --------------------------------------------------------------------

    def insert_run(
        self, run_id: str, started_ns: int, karb_version: str, config: Mapping[str, Any]
    ) -> None:
        self._connection.execute(
            "INSERT INTO runs VALUES (?, ?, NULL, ?, ?)",
            [run_id, started_ns, karb_version, json.dumps(config, sort_keys=True)],
        )

    def finish_run(self, run_id: str, finished_ns: int) -> None:
        self._connection.execute(
            "UPDATE runs SET finished_ns = ? WHERE run_id = ?", [finished_ns, run_id]
        )

    def insert_cycle(
        self,
        run_id: str,
        cycle_no: int,
        started_ns: int,
        finished_ns: int,
        *,
        rescreened: bool,
        screened: int,
        untradeable: int,
        targets: int,
        fetch_errors: int,
    ) -> None:
        self._connection.execute(
            "INSERT INTO cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                cycle_no,
                started_ns,
                finished_ns,
                rescreened,
                screened,
                untradeable,
                targets,
                fetch_errors,
            ],
        )

    def insert_payload(
        self, payload_hash: str, event_ticker: str, recorded_ns: int, payload_json: str
    ) -> None:
        """Content-addressed: an event whose structure has not changed is stored once."""
        self._connection.execute(
            "INSERT OR IGNORE INTO event_payloads VALUES (?, ?, ?, ?)",
            [payload_hash, event_ticker, recorded_ns, payload_json],
        )

    def insert_observations(self, rows: Sequence[ObservationRow]) -> None:
        self._insert_rows("observations", _OBSERVATION_COLUMNS, [astuple(row) for row in rows])

    def insert_books(self, run_id: str, cycle_no: int, books: Mapping[str, OrderBook]) -> None:
        self._insert_rows(
            "books",
            _BOOK_COLUMNS,
            [
                (run_id, cycle_no, ticker, *book_to_row(book))
                for ticker, book in sorted(books.items())
            ],
        )

    def insert_screen_hits(
        self, run_id: str, cycle_no: int, rows: Sequence[tuple[str, str, str, int, str]]
    ) -> None:
        """Rows of (group_key, tier, rule, gross_edge, detail)."""
        self._insert_rows(
            "screen_hits", _SCREEN_HIT_COLUMNS, [(run_id, cycle_no, *row) for row in rows]
        )

    def insert_solver_results(
        self, source: str, run_id: str, cycle_no: int, rows: Sequence[tuple[str, str, str, float]]
    ) -> None:
        """Rows of (group_key, tier, status, pre-rounding profit)."""
        self._insert_rows(
            "solver_results", _SOLVER_COLUMNS, [(source, run_id, cycle_no, *row) for row in rows]
        )

    def insert_opportunities(
        self,
        source: str,
        run_id: str,
        cycle_no: int,
        group_key: str,
        opportunities: Sequence[Opportunity],
    ) -> None:
        self._insert_rows(
            "opportunities",
            _OPPORTUNITY_COLUMNS,
            [
                (
                    source,
                    run_id,
                    cycle_no,
                    group_key,
                    opportunity.id,
                    opportunity.kind.value,
                    opportunity.tier.value,
                    to_ns(opportunity.observed_at),
                    None if opportunity.expires is None else to_ns(opportunity.expires),
                    opportunity.cost.raw,
                    opportunity.basket.fees.raw,
                    opportunity.guaranteed_pnl.raw,
                    opportunity.basket.best_pnl.raw,
                    max((leg.order.qty.raw for leg in opportunity.basket.legs), default=0),
                    legs_json(opportunity),
                )
                for opportunity in opportunities
            ],
        )

    def insert_replay(
        self,
        replay_id: str,
        run_id: str,
        created_ns: int,
        karb_version: str,
        config: Mapping[str, Any],
    ) -> None:
        self._connection.execute(
            "INSERT INTO replays VALUES (?, ?, ?, ?, ?)",
            [replay_id, run_id, created_ns, karb_version, json.dumps(config, sort_keys=True)],
        )

    # ---- writing: paper trading -----------------------------------------------------------------

    def insert_paper_session(self, session: PaperSessionRow) -> None:
        self._connection.execute(
            "INSERT INTO paper_sessions VALUES (?, ?, ?, ?, ?, ?)",
            [
                session.paper_id,
                session.run_id,
                session.kind,
                session.created_ns,
                session.karb_version,
                json.dumps(session.config, sort_keys=True),
            ],
        )

    def insert_paper_trade(self, trade: PaperTradeRow, orders: Sequence[PaperOrderRow]) -> None:
        with self.transaction():
            self._insert_rows("paper_trades", _PAPER_TRADE_COLUMNS, [astuple(trade)])
            self._insert_rows("paper_orders", _PAPER_ORDER_COLUMNS, [astuple(o) for o in orders])

    def settle_paper_trade(
        self,
        trade_id: str,
        *,
        settled_ns: int,
        payout: int,
        realized_pnl: int,
        model_violation: bool,
    ) -> None:
        self._connection.execute(
            """
            UPDATE paper_trades
            SET status = 'settled', settled_ns = ?, payout = ?, realized_pnl = ?,
                model_violation = ?
            WHERE trade_id = ?
            """,
            [settled_ns, payout, realized_pnl, model_violation, trade_id],
        )

    def upsert_settlements(self, rows: Sequence[SettlementRow]) -> None:
        self._insert_rows(
            "settlements", _SETTLEMENT_COLUMNS, [astuple(row) for row in rows], replace=True
        )

    # ---- reading: recordings --------------------------------------------------------------------

    def runs(self) -> list[RunInfo]:
        rows = self._connection.execute(
            """
            SELECT r.run_id, r.started_ns, r.finished_ns, r.karb_version, r.config,
                   (SELECT COUNT(*) FROM cycles c WHERE c.run_id = r.run_id),
                   (SELECT COUNT(*) FROM observations o WHERE o.run_id = r.run_id),
                   (SELECT COUNT(*) FROM opportunities p
                     WHERE p.run_id = r.run_id AND p.source = ?)
            FROM runs r
            ORDER BY r.started_ns, r.run_id
            """,
            [LIVE_SOURCE],
        ).fetchall()
        return [
            RunInfo(
                run_id=row[0],
                started_ns=row[1],
                finished_ns=row[2],
                karb_version=row[3],
                config=json.loads(row[4]),
                cycles=row[5],
                observations=row[6],
                live_opportunities=row[7],
            )
            for row in rows
        ]

    def run(self, run_id: str) -> RunInfo:
        for info in self.runs():
            if info.run_id == run_id:
                return info
        raise StoreError(f"no run {run_id!r} in {self.path}")

    def latest_run_id(self) -> str | None:
        row = self._connection.execute(
            "SELECT run_id FROM runs ORDER BY started_ns DESC, run_id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row[0])

    def cycle_counts(self, run_id: str) -> tuple[int, int]:
        """(cycles, cycles that re-ran the screens)."""
        row = self._connection.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE rescreened) FROM cycles WHERE run_id = ?",
            [run_id],
        ).fetchone()
        return (0, 0) if row is None else (int(row[0]), int(row[1]))

    def observations(self, run_id: str) -> list[ObservationRow]:
        rows = self._connection.execute(
            """
            SELECT run_id, cycle_no, group_key, event_ticker, series_ticker, payload_hash,
                   series_fee_type, series_fee_multiplier, observed_ns, skew_ns, tradeable
            FROM observations
            WHERE run_id = ?
            ORDER BY cycle_no, group_key
            """,
            [run_id],
        ).fetchall()
        return [
            ObservationRow(
                run_id=row[0],
                cycle_no=row[1],
                group_key=row[2],
                event_ticker=row[3],
                series_ticker=row[4],
                payload_hash=row[5],
                series_fee_type=row[6],
                series_fee_multiplier=row[7],
                observed_ns=row[8],
                skew_ns=row[9],
                tradeable=tuple(row[10]),
            )
            for row in rows
        ]

    def books(self, run_id: str, cycle_no: int) -> dict[str, OrderBook]:
        rows = self._connection.execute(
            """
            SELECT ticker, yes_prices, yes_qtys, no_prices, no_qtys
            FROM books
            WHERE run_id = ? AND cycle_no = ?
            """,
            [run_id, cycle_no],
        ).fetchall()
        return {row[0]: book_from_row(row[0], row[1], row[2], row[3], row[4]) for row in rows}

    def payload(self, payload_hash: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload FROM event_payloads WHERE payload_hash = ?", [payload_hash]
        ).fetchone()
        if row is None:
            raise StoreError(f"event payload {payload_hash[:12]} is missing from {self.path}")
        payload = load_json(row[0])
        if not isinstance(payload, dict):
            raise StoreError(f"event payload {payload_hash[:12]} is not a JSON object")
        return payload

    def opportunities(self, source: str, run_id: str) -> list[OpportunityRow]:
        rows = self._connection.execute(
            """
            SELECT source, run_id, cycle_no, group_key, opportunity_id, kind, tier, observed_ns,
                   expires_ns, cost, fees, guaranteed_pnl, best_pnl, contracts
            FROM opportunities
            WHERE source = ? AND run_id = ?
            ORDER BY cycle_no, group_key, opportunity_id
            """,
            [source, run_id],
        ).fetchall()
        return [OpportunityRow(*row) for row in rows]

    def solver_positive(self, source: str, run_id: str, tolerance: float) -> dict[str, int]:
        """Observations per tier whose LP found profit above ``tolerance`` before rounding."""
        rows = self._connection.execute(
            """
            SELECT tier, COUNT(*) FROM solver_results
            WHERE source = ? AND run_id = ? AND profit > ?
            GROUP BY tier ORDER BY tier
            """,
            [source, run_id, tolerance],
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def solver_positive_observations(self, source: str, run_id: str, tolerance: float) -> int:
        """Observations whose LP found profit above ``tolerance`` in at least one tier."""
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT cycle_no, group_key FROM solver_results
                WHERE source = ? AND run_id = ? AND profit > ?
            )
            """,
            [source, run_id, tolerance],
        ).fetchone()
        return 0 if row is None else int(row[0])

    def cycle_times(self, run_id: str) -> list[tuple[int, int]]:
        """(started_ns, finished_ns) of each recorded cycle, in order."""
        rows = self._connection.execute(
            "SELECT started_ns, finished_ns FROM cycles WHERE run_id = ? ORDER BY cycle_no",
            [run_id],
        ).fetchall()
        return [(int(row[0]), int(row[1])) for row in rows]

    def verified_observations(self, source: str, run_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT cycle_no, group_key FROM opportunities
                WHERE source = ? AND run_id = ?
            )
            """,
            [source, run_id],
        ).fetchone()
        return 0 if row is None else int(row[0])

    def screen_hit_counts(self, run_id: str) -> list[tuple[str, str, int, int]]:
        """(tier, rule, hits, distinct groups), most frequent first."""
        rows = self._connection.execute(
            """
            SELECT tier, rule, COUNT(*), COUNT(DISTINCT group_key) FROM screen_hits
            WHERE run_id = ?
            GROUP BY tier, rule
            ORDER BY COUNT(*) DESC, tier, rule
            """,
            [run_id],
        ).fetchall()
        return [(str(row[0]), str(row[1]), int(row[2]), int(row[3])) for row in rows]

    def replays(self, run_id: str) -> list[tuple[str, int, dict[str, Any]]]:
        rows = self._connection.execute(
            "SELECT replay_id, created_ns, config FROM replays WHERE run_id = ? ORDER BY created_ns",
            [run_id],
        ).fetchall()
        return [(str(row[0]), int(row[1]), json.loads(row[2])) for row in rows]

    def table_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in _TABLES:
            if table in _PAPER_TABLES and not self.has_paper_tables:
                counts[table] = 0
                continue
            row = self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = 0 if row is None else int(row[0])
        return counts

    # ---- reading: paper trading -----------------------------------------------------------------

    def paper_sessions(self) -> list[PaperSessionRow]:
        if not self.has_paper_tables:
            return []
        rows = self._connection.execute(
            """
            SELECT paper_id, run_id, kind, created_ns, karb_version, config
            FROM paper_sessions ORDER BY created_ns, paper_id
            """
        ).fetchall()
        return [
            PaperSessionRow(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]))
            for row in rows
        ]

    def paper_trades(
        self, *, paper_id: str | None = None, status: str | None = None
    ) -> list[PaperTradeRow]:
        if not self.has_paper_tables:
            return []
        names = ", ".join(name for name, _ in _PAPER_TRADE_COLUMNS)
        clauses: list[str] = []
        params: list[object] = []
        if paper_id is not None:
            clauses.append("paper_id = ?")
            params.append(paper_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT {names} FROM paper_trades {where} ORDER BY decided_ns, trade_id", params
        ).fetchall()
        return [PaperTradeRow(*row) for row in rows]

    def paper_orders(self, trade_id: str) -> list[PaperOrderRow]:
        if not self.has_paper_tables:
            return []
        names = ", ".join(name for name, _ in _PAPER_ORDER_COLUMNS)
        rows = self._connection.execute(
            f"SELECT {names} FROM paper_orders WHERE trade_id = ? ORDER BY phase, seq",
            [trade_id],
        ).fetchall()
        return [PaperOrderRow(*row) for row in rows]

    def settlements(self, tickers: Sequence[str]) -> dict[str, SettlementRow]:
        if not self.has_paper_tables or not tickers:
            return {}
        names = ", ".join(name for name, _ in _SETTLEMENT_COLUMNS)
        # A list bound as a parameter is slow to convert (see the module docstring); JSON is not.
        rows = self._connection.execute(
            f"""
            SELECT {names} FROM settlements
            WHERE ticker IN (SELECT UNNEST(from_json(?::JSON, '["VARCHAR"]')))
            """,
            [json.dumps(list(tickers))],
        ).fetchall()
        return {row[0]: SettlementRow(*row) for row in rows}
