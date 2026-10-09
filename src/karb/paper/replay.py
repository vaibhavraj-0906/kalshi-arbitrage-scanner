"""Paper trading over a recording (docs/decisions/ADR-0008).

The same trade lifecycle as live paper trading, with recorded books standing in for re-fetched
ones: a decision at a group's observation k fills at its observation k + n and is repaired at
k + 2n, where n is ``latency_cycles``. Latency is therefore the recording's confirmation cadence
-- seconds, not milliseconds -- which makes replayed execution deliberately pessimistic.

Deterministic: the same recording and configuration always produce the same trades.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from karb import __version__
from karb.arb.detect import DetectConfig
from karb.core.fixed import Cash
from karb.market.book import OrderBook
from karb.paper.trade import (
    PaperConfig,
    TradeOutcome,
    complete_trade,
    paper_config_to_json,
    plan_trade,
    trade_rows,
)
from karb.store.codec import detect_config_to_json, from_ns, to_ns
from karb.store.database import ObservationRow, PaperSessionRow, RecordStore, new_id
from karb.store.replay import Replayer

__all__ = ["PaperReplay", "simulate_paper"]


@dataclass
class PaperReplay:
    paper_id: str
    run_id: str
    decisions: int = 0
    """Observations with a verified opportunity."""
    outcomes: list[TradeOutcome] = field(default_factory=list)
    skips: Counter[str] = field(default_factory=Counter)


def simulate_paper(
    store: RecordStore,
    run_id: str,
    detect_config: DetectConfig,
    config: PaperConfig,
    *,
    latency_cycles: int = 1,
    asserted_exhaustive: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> PaperReplay:
    if latency_cycles < 1:
        raise ValueError("latency is at least one recorded observation")
    rows = store.observations(run_id)
    timeline: dict[str, list[ObservationRow]] = defaultdict(list)
    for row in rows:
        timeline[row.group_key].append(row)
    position = {
        (group, row.cycle_no): index
        for group, series in timeline.items()
        for index, row in enumerate(series)
    }

    moment = now or datetime.now(UTC)
    result = PaperReplay(new_id(moment), run_id)
    store.insert_paper_session(
        PaperSessionRow(
            result.paper_id,
            run_id,
            "replay",
            to_ns(moment),
            __version__,
            {
                "paper": paper_config_to_json(config),
                "detect": detect_config_to_json(detect_config),
                "latency_cycles": latency_cycles,
                "asserted_exhaustive": sorted(asserted_exhaustive),
            },
        )
    )
    replayer = Replayer(store, run_id, detect_config, asserted_exhaustive=asserted_exhaustive)
    traded: set[str] = set()
    reserved: dict[str, Cash] = {}

    def later(row: ObservationRow, steps: int) -> ObservationRow | None:
        series = timeline[row.group_key]
        index = position[(row.group_key, row.cycle_no)] + steps
        return series[index] if index < len(series) else None

    def books_at(
        row: ObservationRow | None, tickers: tuple[str, ...]
    ) -> dict[str, OrderBook] | None:
        if row is None:
            return None
        books = replayer.cycle_books(row.cycle_no)
        return {ticker: books[ticker] for ticker in tickers if ticker in books}

    for row in rows:
        replayed = replayer.detect(row)
        if replayed is None:
            continue
        snapshot, detection = replayed
        if not detection.opportunities:
            continue
        result.decisions += 1
        if row.group_key in traded:
            result.skips["group already traded this run"] += 1
            continue
        plan = plan_trade(
            detection.opportunities[0], snapshot, row.group_key, detect_config, config
        )
        if isinstance(plan, str):
            result.skips[plan] += 1
            continue
        if Cash.total(reserved.values()) + plan.planned_cost > config.capital:
            result.skips["capital limit"] += 1
            continue
        traded.add(row.group_key)

        entry_row = later(row, latency_cycles)
        hedge_row = later(row, 2 * latency_cycles)
        outcome = complete_trade(
            plan,
            decided_at=snapshot.observed_at,
            entry_books=books_at(entry_row, plan.tickers),
            entry_at=None if entry_row is None else from_ns(entry_row.observed_ns),
            hedge_books=books_at(hedge_row, plan.tickers),
            hedge_at=None if hedge_row is None else from_ns(hedge_row.observed_ns),
            tradeable=frozenset(entry_row.tradeable) if entry_row else snapshot.tradeable,
            fee_config=detect_config.fees,
            max_levels=detect_config.max_levels,
            hedge=config.hedge,
            note="" if entry_row is not None else "recording ends before the arrival book",
        )
        trade, orders = trade_rows(
            outcome,
            trade_id=f"{result.paper_id}-{len(result.outcomes) + 1:04d}",
            paper_id=result.paper_id,
            run_id=run_id,
            cycle_no=row.cycle_no,
        )
        store.insert_paper_trade(trade, orders)
        result.outcomes.append(outcome)
        if outcome.status == "open":
            reserved[row.group_key] = outcome.entry_cost + outcome.hedge_cost
    return result
