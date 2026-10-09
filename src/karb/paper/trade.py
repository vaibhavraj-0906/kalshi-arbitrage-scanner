"""One paper trade, from decision to attribution (docs/decisions/ADR-0008).

Timeline, with the same lag at every step a real taker would face:

    decision book B0  -- the snapshot detection saw: the basket is planned here
    arrival book  B1  -- fetched one latency later: entry orders fill here
    hedge book    B2  -- one latency after that: repair orders, decided on B1, fill here

Each stage acts on information one book old. P&L is attributed exactly, in a chain that telescopes:

    realized = planned + (after_entry - planned) + (after_hedge - after_entry) + (realized - after_hedge)
                         execution                 hedging                      settlement outcome

``planned``, ``after_entry`` and ``after_hedge`` are guaranteed (worst-atom) P&Ls under the
opportunity's outcome model. The settlement outcome can never be negative if the model read the
contracts correctly, so a negative one is flagged as a model violation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from karb.arb.detect import DetectConfig, EventSnapshot
from karb.arb.opportunity import LegFill, Opportunity, VerifiedBasket
from karb.arb.verify import verify_basket
from karb.core.fixed import Cash, Qty
from karb.market.book import OrderBook, Side
from karb.market.fees import FeeConfig, FeeSchedule
from karb.paper.execution import (
    Execution,
    Liquidity,
    Order,
    execute,
    plan_orders,
    position_legs,
)
from karb.paper.hedge import decide_hedge, worst_and_best
from karb.store.codec import dumps_exact, to_ns
from karb.store.database import PaperOrderRow, PaperTradeRow

__all__ = [
    "Attribution",
    "PaperConfig",
    "TradeOutcome",
    "TradePlan",
    "complete_trade",
    "paper_config_to_json",
    "plan_trade",
    "trade_rows",
]


DEFAULT_MAX_COST_PER_TRADE = Cash(100_000_000)
DEFAULT_CAPITAL = Cash(10_000_000_000)


@dataclass(frozen=True, slots=True)
class PaperConfig:
    latency: float = 1.0
    """Seconds between seeing a book and orders arriving at the exchange (live trading)."""
    max_cost_per_trade: Cash = DEFAULT_MAX_COST_PER_TRADE
    """Budget per basket, fees included. Larger baskets are scaled down. Default $100."""
    capital: Cash = DEFAULT_CAPITAL
    """Cash that open positions may tie up in total. Default $10,000."""
    hedge: bool = True
    """Repair half-filled baskets; off means holding whatever filled."""

    def __post_init__(self) -> None:
        if self.latency < 0:
            raise ValueError("latency cannot be negative")
        if self.max_cost_per_trade.raw <= 0 or self.capital.raw <= 0:
            raise ValueError("budgets must be positive")


def paper_config_to_json(config: PaperConfig) -> dict[str, Any]:
    return {
        "latency": config.latency,
        "max_cost_per_trade": config.max_cost_per_trade.raw,
        "capital": config.capital.raw,
        "hedge": config.hedge,
    }


@dataclass(frozen=True, slots=True)
class TradePlan:
    opportunity: Opportunity
    group_key: str
    fees: FeeSchedule
    basket: VerifiedBasket
    """The basket to send, scaled to budget and priced exactly on the decision book."""
    orders: tuple[Order, ...]

    @property
    def planned_pnl(self) -> Cash:
        return self.basket.guaranteed_pnl

    @property
    def planned_cost(self) -> Cash:
        return self.basket.cost

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted({order.ticker for order in self.orders}))


def plan_trade(
    opportunity: Opportunity,
    snapshot: EventSnapshot,
    group_key: str,
    detect_config: DetectConfig,
    paper_config: PaperConfig,
) -> TradePlan | str:
    """The orders to send for ``opportunity``, or the reason there are none."""
    budget = paper_config.max_cost_per_trade
    fees = snapshot.structure.fees
    legs = opportunity.basket.legs
    cost = opportunity.cost
    basket: VerifiedBasket | None = None
    for shave in (0, 1):
        quantities: dict[tuple[str, Side], Qty] = {}
        for leg in legs:
            raw = leg.order.qty.raw
            if cost > budget:
                raw = raw * budget.raw // cost.raw
            whole = Qty(raw).floor_whole().raw - shave * Qty.contracts(1).raw
            if whole > 0:
                quantities[(leg.ticker, leg.side)] = Qty(whole)
        if len(quantities) != len(legs):
            return "a leg rounds to zero contracts within the budget"
        basket = verify_basket(
            opportunity.space, snapshot.books, quantities, fees, detect_config.fees
        )
        if basket.cost <= budget:
            break
    if basket is None or basket.cost > budget:
        return "cannot fit the basket within the per-trade budget"
    if basket.guaranteed_pnl < detect_config.min_profit:
        return "edge below the minimum at the budgeted size"
    return TradePlan(opportunity, group_key, fees, basket, plan_orders(basket))


@dataclass(frozen=True, slots=True)
class Attribution:
    planned: Cash
    after_entry: Cash
    after_hedge: Cash
    realized: Cash | None = None

    @property
    def execution(self) -> Cash:
        """Latency, price moves, missing size and leg imbalance, all at entry."""
        return self.after_entry - self.planned

    @property
    def hedging(self) -> Cash:
        return self.after_hedge - self.after_entry

    @property
    def outcome(self) -> Cash | None:
        """What settlement paid above the guaranteed worst case. Never negative if the model
        read the contracts correctly."""
        return None if self.realized is None else self.realized - self.after_hedge

    @property
    def model_violation(self) -> bool:
        return self.realized is not None and self.realized < self.after_hedge


@dataclass(frozen=True, slots=True)
class TradeOutcome:
    plan: TradePlan
    decided_at: datetime
    entry_at: datetime | None
    hedge_at: datetime | None
    entry: tuple[Execution, ...]
    hedge: tuple[Execution, ...]
    attribution: Attribution
    best_after_hedge: Cash
    status: str
    note: str

    @property
    def position(self) -> tuple[LegFill, ...]:
        return position_legs(self.entry, self.hedge)

    @property
    def entry_cost(self) -> Cash:
        return Cash.total(e.cash_out for e in self.entry)

    @property
    def hedge_cost(self) -> Cash:
        return Cash.total(e.cash_out for e in self.hedge)

    @property
    def planned_contracts(self) -> Qty:
        total = Qty.ZERO
        for order in self.plan.orders:
            total = total + order.qty
        return total

    @property
    def filled_contracts(self) -> Qty:
        total = Qty.ZERO
        for execution in self.entry:
            total = total + execution.filled
        return total


def complete_trade(
    plan: TradePlan,
    *,
    decided_at: datetime,
    entry_books: Mapping[str, OrderBook] | None,
    entry_at: datetime | None,
    hedge_books: Mapping[str, OrderBook] | None,
    hedge_at: datetime | None,
    tradeable: frozenset[str],
    fee_config: FeeConfig,
    max_levels: int,
    hedge: bool,
    note: str = "",
) -> TradeOutcome:
    """Execute ``plan`` on the arrival book, repair it on the next, and attribute the result."""
    space = plan.opportunity.space
    planned = plan.planned_pnl
    if entry_books is None:
        return TradeOutcome(
            plan,
            decided_at,
            None,
            None,
            (),
            (),
            Attribution(planned, Cash.ZERO, Cash.ZERO),
            Cash.ZERO,
            "missed",
            note or "no book after the latency",
        )

    liquidity = Liquidity()
    entry = execute(plan.orders, entry_books, liquidity, plan.fees, fee_config)
    entry_legs = position_legs(entry)
    after_entry, _ = worst_and_best(space, entry_legs)

    notes = [note] if note else []
    hedge_executions: tuple[Execution, ...] = ()
    if hedge and entry_legs:
        orders = decide_hedge(
            space,
            entry_books,
            liquidity,
            entry_legs,
            tradeable,
            plan.fees,
            fee_config,
            max_levels=max_levels,
        )
        if orders and hedge_books is None:
            notes.append("hedge decided but no later book arrived: left unhedged")
        elif orders and hedge_books is not None:
            hedge_executions = execute(orders, hedge_books, liquidity, plan.fees, fee_config)
    position = position_legs(entry, hedge_executions)
    after_hedge, best = worst_and_best(space, position)

    filled = sum(e.filled.raw for e in entry)
    ordered = sum(order.qty.raw for order in plan.orders)
    if filled < ordered:
        notes.append(f"entry filled {filled / ordered:.0%}")
    if hedge_executions:
        notes.append(f"hedged with {len(hedge_executions)} order(s)")
    return TradeOutcome(
        plan,
        decided_at,
        entry_at,
        hedge_at if hedge_executions else None,
        entry,
        hedge_executions,
        Attribution(planned, after_entry, after_hedge),
        best,
        "open" if position else "flat",
        "; ".join(notes),
    )


def _fills_json(fill: LegFill | None) -> str:
    if fill is None:
        return "[]"
    return dumps_exact(
        [
            {
                "price": f.price.raw,
                "qty": f.qty.raw,
                "trade_fee": f.trade_fee.raw,
                "rounding_fee": f.rounding_fee.raw,
                "rebate": f.rebate.raw,
            }
            for f in fill.order.fills
        ]
    )


def trade_rows(
    outcome: TradeOutcome, *, trade_id: str, paper_id: str, run_id: str, cycle_no: int
) -> tuple[PaperTradeRow, list[PaperOrderRow]]:
    plan = outcome.plan
    opportunity = plan.opportunity
    attribution = outcome.attribution
    flat = outcome.status in ("flat", "missed")
    trade = PaperTradeRow(
        trade_id=trade_id,
        paper_id=paper_id,
        run_id=run_id,
        cycle_no=cycle_no,
        group_key=plan.group_key,
        event_ticker=opportunity.event_ticker,
        opportunity_id=opportunity.id,
        kind=opportunity.kind.value,
        tier=opportunity.tier.value,
        decided_ns=to_ns(outcome.decided_at),
        entry_ns=None if outcome.entry_at is None else to_ns(outcome.entry_at),
        hedge_ns=None if outcome.hedge_at is None else to_ns(outcome.hedge_at),
        planned_cost=plan.planned_cost.raw,
        planned_pnl=attribution.planned.raw,
        entry_cost=outcome.entry_cost.raw,
        worst_after_entry=attribution.after_entry.raw,
        hedge_cost=outcome.hedge_cost.raw,
        worst_after_hedge=attribution.after_hedge.raw,
        best_after_hedge=outcome.best_after_hedge.raw,
        planned_contracts=outcome.planned_contracts.raw,
        filled_contracts=outcome.filled_contracts.raw,
        status=outcome.status,
        note=outcome.note,
        # Nothing held means nothing to settle: the result is final at zero.
        payout=0 if flat else None,
        realized_pnl=0 if flat else None,
        model_violation=False if flat else None,
    )
    orders: list[PaperOrderRow] = []
    for seq, (order, leg) in enumerate(zip(plan.orders, plan.basket.legs, strict=True)):
        orders.append(
            PaperOrderRow(
                trade_id,
                "plan",
                seq,
                order.ticker,
                order.side.value,
                order.limit.raw,
                order.qty.raw,
                leg.order.qty.raw,
                leg.order.cash_out.raw,
                leg.order.fees.raw,
                _fills_json(leg),
            )
        )
    for phase, executions in (("entry", outcome.entry), ("hedge", outcome.hedge)):
        for seq, execution in enumerate(executions):
            order = execution.order
            orders.append(
                PaperOrderRow(
                    trade_id,
                    phase,
                    seq,
                    order.ticker,
                    order.side.value,
                    order.limit.raw,
                    order.qty.raw,
                    execution.filled.raw,
                    execution.cash_out.raw,
                    execution.fees.raw,
                    _fills_json(execution.fill),
                )
            )
    return trade, orders
