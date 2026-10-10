"""Trading verified opportunities on the exchange while scanning (docs/decisions/ADR-0010).

Each verified opportunity becomes a trade on its first sighting. Trades run one at a time, in
the background, in the order they were found:

1. check the account: no position already held in the basket's markets, enough cash;
2. send every leg at once as immediate-or-cancel buys, and read back the exact fills;
3. if the legs filled unevenly, fetch fresh books and send the repair the basket LP finds;
4. read the balance and positions again and audit the trade against them;
5. record the trade, its orders and its attribution.

One trade at a time keeps the audit exact: the balance moves only because of this trade, and the
positions it checks were flat before it. At most one trade per event group per run.

The audit halts trading -- no further orders this session -- when the exchange disagrees with
the model: an order error karb cannot resolve, fees above what the fee model allowed (the
guarantees would be overstated), or positions that do not match the fills. A stop file (by
default ``data/STOP``) halts trading too, without stopping the scan.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from karb import __version__
from karb.arb.detect import DetectConfig, EventSnapshot
from karb.core.fixed import Cash
from karb.exchange.client import KalshiClient, KalshiError
from karb.exchange.endpoints import fetch_orderbooks, pack_batches
from karb.market.book import OrderBook, Side
from karb.scanner.service import CycleReport
from karb.store.codec import detect_config_to_json, to_ns
from karb.store.database import RecordStore, SessionRow, new_id
from karb.trading.hedge import decide_hedge
from karb.trading.orders import PlacedOrder, position_legs
from karb.trading.plan import (
    TradeConfig,
    TradeOutcome,
    TradePlan,
    assemble_outcome,
    plan_trade,
    trade_config_to_json,
    trade_rows,
)
from karb.trading.portfolio import fetch_balance, fetch_positions, place_orders

__all__ = ["Trader", "audit_trade"]


@dataclass(frozen=True, slots=True)
class _Queued:
    plan: TradePlan
    snapshot: EventSnapshot
    cycle_no: int
    sequence: int


def audit_trade(
    outcome: TradeOutcome,
    positions_after: Mapping[str, int] | None,
    model_fees: Cash,
) -> list[str]:
    """Every way the exchange's account of ``outcome`` disagrees with the model. Positions are
    not checked when they could not be read (``None``)."""
    problems = [f"{p.order.ticker}: {p.error}" for p in outcome.orders if p.error]
    charged = Cash.total(p.fees for p in outcome.orders)
    if outcome.balance_change is not None:
        expected = outcome.netted_cash - outcome.entry_cost - outcome.hedge_cost
        shortfall = expected - outcome.balance_change  # positive: the account paid more
        if shortfall > Cash.ZERO:
            charged = charged + shortfall
    if charged > model_fees:
        problems.append(
            f"the exchange charged {charged.dollars(6)} in fees and rounding where the fee model "
            f"allowed {model_fees.dollars(6)}"
        )
    if positions_after is None:
        return problems
    expected_positions: dict[str, int] = defaultdict(int)
    for leg in outcome.position:
        signed = leg.order.qty.raw if leg.side is Side.YES else -leg.order.qty.raw
        expected_positions[leg.ticker] += signed
    tickers = {o.ticker for o in outcome.plan.orders} | {p.order.ticker for p in outcome.hedge}
    for ticker in sorted(tickers):
        held, want = positions_after.get(ticker, 0), expected_positions.get(ticker, 0)
        if held != want:
            problems.append(
                f"{ticker}: the exchange shows a position of {held / 100:g}, the fills {want / 100:g}"
            )
    return problems


class Trader:
    def __init__(
        self,
        client: KalshiClient,
        store: RecordStore,
        *,
        run_id: str,
        detect_config: DetectConfig,
        config: TradeConfig,
        environment: str,
        stop_file: Path | None = None,
    ) -> None:
        if not client.authenticated:
            raise ValueError("trading needs a client with credentials")
        self.client = client
        self.store = store
        self.run_id = run_id
        self.detect_config = detect_config
        self.config = config
        self.environment = environment
        self.stop_file = stop_file
        now = client.clock.now()
        self.session_id = new_id(now)
        store.insert_session(
            SessionRow(
                self.session_id,
                run_id,
                environment,
                to_ns(now),
                __version__,
                {
                    "trade": trade_config_to_json(config),
                    "detect": detect_config_to_json(detect_config),
                    "exchange": client.base_url,
                },
            )
        )
        self.outcomes: list[TradeOutcome] = []
        self.skips: Counter[str] = Counter()
        self.errors: list[str] = []
        self.halted: str | None = None
        self._traded: set[str] = set()
        self._reserved: dict[str, Cash] = {}
        self._queue: deque[_Queued] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._in_flight = False
        self._sequence = 0

    @property
    def capital_in_use(self) -> Cash:
        return Cash.total(self._reserved.values())

    @property
    def pending(self) -> int:
        return len(self._queue) + int(self._in_flight)

    def halt(self, reason: str) -> None:
        if self.halted is None:
            self.halted = reason

    def _check_stop_file(self) -> None:
        if self.stop_file is not None and self.stop_file.exists():
            self.halt(f"stop file {self.stop_file} exists")

    def consider(self, report: CycleReport, cycle_no: int) -> None:
        """Queue a trade for every newly seen opportunity the limits allow."""
        self._check_stop_file()
        for group_key, detection in sorted(report.detections.items()):
            if not detection.opportunities:
                continue
            if self.halted is not None:
                self.skips["trading halted"] += 1
                continue
            if group_key in self._traded:
                self.skips["group already traded this run"] += 1
                continue
            if self.config.max_trades is not None and self._sequence >= self.config.max_trades:
                self.skips["trade limit reached"] += 1
                continue
            snapshot = report.snapshots[group_key]
            plan = plan_trade(
                detection.opportunities[0], snapshot, group_key, self.detect_config, self.config
            )
            if isinstance(plan, str):
                self.skips[plan] += 1
                continue
            self.submit(plan, snapshot, cycle_no)

    def submit(self, plan: TradePlan, snapshot: EventSnapshot, cycle_no: int) -> bool:
        """Queue ``plan``; ``False`` if a limit refuses it."""
        if self.capital_in_use + plan.planned_cost > self.config.capital:
            self.skips["capital limit"] += 1
            return False
        self._traded.add(plan.group_key)
        self._reserved[plan.group_key] = plan.planned_cost
        self._sequence += 1
        self._queue.append(_Queued(plan, snapshot, cycle_no, self._sequence))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work())
        return True

    def cancel_queued(self) -> int:
        """Drop every trade not yet sent; the one in flight, if any, still finishes."""
        dropped = list(self._queue)
        self._queue.clear()
        for item in dropped:
            self._release(item, "scan stopped before the trade was sent")
        return len(dropped)

    async def drain(self) -> None:
        """Wait for every queued trade to finish."""
        while self._worker is not None and not self._worker.done():
            await self._worker

    async def _work(self) -> None:
        while self._queue:
            item = self._queue.popleft()
            self._in_flight = True
            try:
                await self._execute(item)
            except Exception as exc:  # one failed trade must not stop a long scan
                self._reserved.pop(item.plan.group_key, None)
                self.errors.append(f"{item.plan.group_key}: {type(exc).__name__}: {exc}")
                # Orders may have gone out: the account is in an unknown state until checked.
                self.halt(f"trade {item.sequence} failed: {type(exc).__name__}: {exc}")
            finally:
                self._in_flight = False

    def _release(self, item: _Queued, reason: str) -> None:
        self._reserved.pop(item.plan.group_key, None)
        self.skips[reason] += 1

    async def _execute(self, item: _Queued) -> TradeOutcome | None:
        plan, snapshot = item.plan, item.snapshot
        self._check_stop_file()
        if self.halted is not None:
            self._release(item, "trading halted")
            return None
        event_ticker = plan.opportunity.event_ticker.split("#", 1)[0]  # the exchange's event
        try:
            held = await fetch_positions(self.client, event_ticker)
            before = await fetch_balance(self.client)
        except KalshiError as exc:  # nothing has been sent yet, so nothing is at risk
            self._release(item, f"account check failed: {type(exc).__name__}")
            return None
        if any(held.get(ticker, 0) for ticker in plan.tickers):
            self._release(item, "account already holds a position in the basket's markets")
            return None
        if before < plan.planned_cost:
            self._release(item, "balance below the basket's cost")
            return None

        trade_id = f"{self.session_id}-{item.sequence:04d}"
        since = self.client.clock.now() - timedelta(seconds=5)
        entry = await place_orders(
            self.client,
            plan.orders,
            trade_id=trade_id,
            phase="entry",
            since=since,
            batch_size=self.config.batch_size,
            balance_unit=self.detect_config.fees.balance_unit,
        )
        entry_at = self.client.clock.now()
        hedge: tuple[PlacedOrder, ...] = ()
        hedge_at = None
        notes: list[str] = []
        incomplete = any(p.filled < p.order.qty for p in entry)
        entry_legs = position_legs(entry)
        if self.config.hedge and incomplete and entry_legs:
            books, note = await self._books(plan.tickers)
            if books is None:
                notes.append(f"{note}: left unrepaired")
            else:
                orders = decide_hedge(
                    plan.opportunity.space,
                    books,
                    entry_legs,
                    snapshot.tradeable,
                    plan.fees,
                    self.detect_config.fees,
                    max_levels=self.detect_config.max_levels,
                )
                if orders:
                    hedge = await place_orders(
                        self.client,
                        orders,
                        trade_id=trade_id,
                        phase="hedge",
                        since=since,
                        batch_size=self.config.batch_size,
                        balance_unit=self.detect_config.fees.balance_unit,
                    )
                    hedge_at = self.client.clock.now()
        # Orders have gone out: from here on, every failure is recorded with the trade.
        after: Cash | None = None
        positions_after: dict[str, int] | None = None
        unread = ""
        try:
            after = await fetch_balance(self.client)
            positions_after = await fetch_positions(self.client, event_ticker)
        except KalshiError as exc:
            unread = f"could not read the account after the trade: {type(exc).__name__}: {exc}"
        outcome = assemble_outcome(
            plan,
            decided_at=snapshot.observed_at,
            entry=entry,
            entry_at=entry_at,
            hedge=hedge,
            hedge_at=hedge_at,
            notes=notes,
            balance_change=None if after is None else after - before,
        )
        model_fees = Cash.total(
            p.model_fees(plan.fees, self.detect_config.fees) for p in outcome.orders
        )
        problems = audit_trade(outcome, positions_after, model_fees)
        if unread:
            problems.append(unread)
        if problems:
            outcome = replace(
                outcome,
                note="; ".join(
                    part for part in (outcome.note, "AUDIT: " + "; ".join(problems)) if part
                ),
            )
        trade, orders_rows = trade_rows(
            outcome,
            trade_id=trade_id,
            session_id=self.session_id,
            run_id=self.run_id,
            cycle_no=item.cycle_no,
            fee_config=self.detect_config.fees,
        )
        self.store.insert_trade(trade, orders_rows)
        self.outcomes.append(outcome)
        if outcome.status == "open":
            self._reserved[plan.group_key] = outcome.entry_cost + outcome.hedge_cost
        else:
            self._reserved.pop(plan.group_key, None)
        if problems:
            self.halt(f"trade {trade_id}: {problems[0]}")
        return outcome

    async def execute(
        self, plan: TradePlan, snapshot: EventSnapshot, *, cycle_no: int = 0
    ) -> TradeOutcome | None:
        """Trade ``plan`` now, outside the scan loop (``karb trade --exercise``)."""
        if self.capital_in_use + plan.planned_cost > self.config.capital:
            self.skips["capital limit"] += 1
            return None
        self._traded.add(plan.group_key)
        self._reserved[plan.group_key] = plan.planned_cost
        self._sequence += 1
        return await self._execute(_Queued(plan, snapshot, cycle_no, self._sequence))

    async def _books(self, tickers: Sequence[str]) -> tuple[dict[str, OrderBook] | None, str]:
        books: dict[str, OrderBook] = {}
        try:
            for batch in pack_batches([list(tickers)]):
                fetched = await fetch_orderbooks(
                    self.client, batch, depth=self.detect_config.max_levels
                )
                books.update(fetched.books)
        except KalshiError as exc:
            return None, f"book fetch failed: {exc}"
        return books, ""
