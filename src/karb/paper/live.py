"""Paper trading alongside a live scan (docs/decisions/ADR-0008).

Each verified opportunity becomes a paper trade on its first sighting -- the arrival book is the
confirmation, so waiting for a second sighting would only add latency. The trade then runs in the
background:

1. sleep one latency, re-fetch the basket's books, and fill the entry orders there;
2. sleep again, re-fetch, and fill any repair orders decided on the arrival book;
3. record the trade, its orders and its attribution.

At most one paper trade per event group per run. The paper trader never moves the real market, so
a lasting violation would otherwise be "traded" every few seconds against the same displayed size.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence

from karb import __version__
from karb.arb.detect import DetectConfig, EventSnapshot
from karb.core.fixed import Cash
from karb.exchange.client import KalshiClient, KalshiError
from karb.exchange.endpoints import fetch_orderbooks, pack_batches
from karb.market.book import OrderBook
from karb.paper.trade import (
    PaperConfig,
    TradeOutcome,
    TradePlan,
    complete_trade,
    paper_config_to_json,
    plan_trade,
    trade_rows,
)
from karb.scanner.service import CycleReport
from karb.store.codec import detect_config_to_json, to_ns
from karb.store.database import PaperSessionRow, RecordStore, new_id

__all__ = ["PaperTrader"]

Sleep = Callable[[float], Awaitable[None]]


class PaperTrader:
    def __init__(
        self,
        client: KalshiClient,
        store: RecordStore,
        *,
        run_id: str,
        detect_config: DetectConfig,
        config: PaperConfig,
        sleep: Sleep | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.run_id = run_id
        self.detect_config = detect_config
        self.config = config
        self._sleep: Sleep = sleep or asyncio.sleep
        now = client.clock.now()
        self.paper_id = new_id(now)
        store.insert_paper_session(
            PaperSessionRow(
                self.paper_id,
                run_id,
                "live",
                to_ns(now),
                __version__,
                {
                    "paper": paper_config_to_json(config),
                    "detect": detect_config_to_json(detect_config),
                },
            )
        )
        self.outcomes: list[TradeOutcome] = []
        self.skips: Counter[str] = Counter()
        self.errors: list[str] = []
        self._traded: set[str] = set()
        self._reserved: dict[str, Cash] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._sequence = 0

    @property
    def capital_in_use(self) -> Cash:
        return Cash.total(self._reserved.values())

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def consider(self, report: CycleReport, cycle_no: int) -> None:
        """Start a paper trade for every newly seen opportunity the risk limits allow."""
        for group_key, detection in sorted(report.detections.items()):
            if not detection.opportunities:
                continue
            if group_key in self._traded:
                self.skips["group already traded this run"] += 1
                continue
            snapshot = report.snapshots[group_key]
            plan = plan_trade(
                detection.opportunities[0], snapshot, group_key, self.detect_config, self.config
            )
            if isinstance(plan, str):
                self.skips[plan] += 1
                continue
            if self.capital_in_use + plan.planned_cost > self.config.capital:
                self.skips["capital limit"] += 1
                continue
            self._traded.add(group_key)
            self._reserved[group_key] = plan.planned_cost
            self._sequence += 1
            task = asyncio.get_running_loop().create_task(
                self._run(plan, snapshot, cycle_no, self._sequence)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every trade in flight to finish."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def _run(
        self, plan: TradePlan, snapshot: EventSnapshot, cycle_no: int, sequence: int
    ) -> None:
        try:
            await self._sleep(self.config.latency)
            entry_books, entry_note = await self._books(plan.tickers)
            entry_at = self.client.clock.now()
            hedge_books: Mapping[str, OrderBook] | None = None
            hedge_note = ""
            if entry_books is not None:
                await self._sleep(self.config.latency)
                hedge_books, hedge_note = await self._books(plan.tickers)
            outcome = complete_trade(
                plan,
                decided_at=snapshot.observed_at,
                entry_books=entry_books,
                entry_at=entry_at if entry_books is not None else None,
                hedge_books=hedge_books,
                hedge_at=self.client.clock.now() if hedge_books is not None else None,
                tradeable=snapshot.tradeable,
                fee_config=self.detect_config.fees,
                max_levels=self.detect_config.max_levels,
                hedge=self.config.hedge,
                note="; ".join(part for part in (entry_note, hedge_note) if part),
            )
            trade, orders = trade_rows(
                outcome,
                trade_id=f"{self.paper_id}-{sequence:04d}",
                paper_id=self.paper_id,
                run_id=self.run_id,
                cycle_no=cycle_no,
            )
            self.store.insert_paper_trade(trade, orders)
            self.outcomes.append(outcome)
            if outcome.status == "open":
                self._reserved[plan.group_key] = outcome.entry_cost + outcome.hedge_cost
            else:
                self._reserved.pop(plan.group_key, None)
        except Exception as exc:  # one failed paper trade must not stop a long scan
            self._reserved.pop(plan.group_key, None)
            self.errors.append(f"{plan.group_key}: {type(exc).__name__}: {exc}")

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
