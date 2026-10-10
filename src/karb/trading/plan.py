"""One trade, from decision to attribution (docs/decisions/ADR-0008, ADR-0010).

Timeline:

    decision book  -- the snapshot detection saw: the basket is planned and sized here
    entry          -- immediate-or-cancel orders for every leg, sent at once; they fill against
                      whatever the exchange holds when they arrive
    repair         -- if the entry filled unevenly, books fetched after it decide the repair,
                      which is sent the same way

P&L is attributed exactly, in a chain that telescopes:

    realized = planned + (after_entry - planned) + (after_hedge - after_entry) + (realized - after_hedge)
                         execution                 hedging                      settlement outcome

``planned``, ``after_entry`` and ``after_hedge`` are guaranteed (worst-atom) P&Ls under the
opportunity's outcome model, priced with the fees the exchange actually charged. The settlement
outcome can never be negative if the model read the contracts correctly, so a negative one is
flagged as a model violation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from karb.arb.detect import DetectConfig, EventSnapshot
from karb.arb.opportunity import LegFill, Opportunity, VerifiedBasket
from karb.arb.verify import verify_basket
from karb.core.fixed import Cash, Qty
from karb.market.book import Side
from karb.market.fees import FeeConfig, FeeSchedule
from karb.store.codec import dumps_exact, to_ns
from karb.store.database import TradeOrderRow, TradeRow
from karb.trading.hedge import worst_and_best
from karb.trading.orders import Order, PlacedOrder, plan_orders, position_legs

__all__ = [
    "Attribution",
    "TradeConfig",
    "TradeOutcome",
    "TradePlan",
    "assemble_outcome",
    "netted_cash",
    "plan_trade",
    "trade_config_to_json",
    "trade_rows",
]


DEFAULT_MAX_COST_PER_TRADE = Cash(100_000_000)
DEFAULT_CAPITAL = Cash(10_000_000_000)


@dataclass(frozen=True, slots=True)
class TradeConfig:
    max_cost_per_trade: Cash = DEFAULT_MAX_COST_PER_TRADE
    """Budget per basket, fees included. Larger baskets are scaled down. Default $100."""
    capital: Cash = DEFAULT_CAPITAL
    """Cash that open positions may tie up in total. Default $10,000."""
    hedge: bool = True
    """Repair half-filled baskets; off means holding whatever filled."""
    max_trades: int | None = None
    """Stop trading after this many trades in a session."""
    batch_size: int = 10
    """Orders per batched request. Kalshi's Basic tier fits ten in its write bucket."""

    def __post_init__(self) -> None:
        if self.max_cost_per_trade.raw <= 0 or self.capital.raw <= 0:
            raise ValueError("budgets must be positive")
        if self.max_trades is not None and self.max_trades < 1:
            raise ValueError("max_trades must be at least 1")
        if not 1 <= self.batch_size <= 10:
            raise ValueError("batch_size must be between 1 and 10")


def trade_config_to_json(config: TradeConfig) -> dict[str, Any]:
    return {
        "max_cost_per_trade": config.max_cost_per_trade.raw,
        "capital": config.capital.raw,
        "hedge": config.hedge,
        "max_trades": config.max_trades,
        "batch_size": config.batch_size,
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
    config: TradeConfig,
) -> TradePlan | str:
    """The orders to send for ``opportunity``, or the reason there are none."""
    budget = config.max_cost_per_trade
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
        """Price moves, missing size and leg imbalance between decision and fill."""
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


def netted_cash(legs: Sequence[LegFill]) -> Cash:
    """Cash the exchange returns at once for YES and NO held in the same market.

    Kalshi keeps one signed position per market, so buying the other side of a held market
    closes pairs at $1 each immediately. The model keeps holding both sides until settlement,
    where they pay the same $1, so only the timing differs.
    """
    held: dict[tuple[str, Side], int] = defaultdict(int)
    for leg in legs:
        held[(leg.ticker, leg.side)] += leg.order.qty.raw
    pairs = sum(min(held[(t, Side.YES)], held[(t, Side.NO)]) for t in {t for t, _ in held})
    return Qty(pairs).payout()


@dataclass(frozen=True, slots=True)
class TradeOutcome:
    plan: TradePlan
    decided_at: datetime
    entry_at: datetime | None
    hedge_at: datetime | None
    entry: tuple[PlacedOrder, ...]
    hedge: tuple[PlacedOrder, ...]
    attribution: Attribution
    best_after_hedge: Cash
    status: str
    note: str
    balance_change: Cash | None = None

    @property
    def position(self) -> tuple[LegFill, ...]:
        return position_legs(self.entry, self.hedge)

    @property
    def entry_cost(self) -> Cash:
        return Cash.total(placed.cash_out for placed in self.entry)

    @property
    def hedge_cost(self) -> Cash:
        return Cash.total(placed.cash_out for placed in self.hedge)

    @property
    def netted_cash(self) -> Cash:
        return netted_cash(self.position)

    @property
    def planned_contracts(self) -> Qty:
        total = Qty.ZERO
        for order in self.plan.orders:
            total = total + order.qty
        return total

    @property
    def filled_contracts(self) -> Qty:
        total = Qty.ZERO
        for placed in self.entry:
            total = total + placed.filled
        return total

    @property
    def orders(self) -> tuple[PlacedOrder, ...]:
        return (*self.entry, *self.hedge)


def assemble_outcome(
    plan: TradePlan,
    *,
    decided_at: datetime,
    entry: Sequence[PlacedOrder],
    entry_at: datetime | None,
    hedge: Sequence[PlacedOrder],
    hedge_at: datetime | None,
    notes: Sequence[str] = (),
    balance_change: Cash | None = None,
) -> TradeOutcome:
    """Attribute what the exchange did with ``plan``'s orders."""
    space = plan.opportunity.space
    after_entry, _ = worst_and_best(space, position_legs(entry))
    after_hedge, best = worst_and_best(space, position_legs(entry, hedge))
    filled = sum(placed.filled.raw for placed in entry)
    ordered = sum(order.qty.raw for order in plan.orders)
    remarks = list(notes)
    if filled < ordered:
        remarks.append(f"entry filled {filled / ordered:.0%}")
    if hedge:
        remarks.append(f"repaired with {len(hedge)} order(s)")
    errors = [p.error for p in (*entry, *hedge) if p.error]
    if errors:
        remarks.append(f"{len(errors)} order error(s): {errors[0]}")
    held = position_legs(entry, hedge)
    return TradeOutcome(
        plan,
        decided_at,
        entry_at,
        hedge_at if hedge else None,
        tuple(entry),
        tuple(hedge),
        Attribution(plan.planned_pnl, after_entry, after_hedge),
        best,
        "open" if held else "flat",
        "; ".join(remarks),
        balance_change,
    )


def _fills_json(leg: LegFill | None) -> str:
    if leg is None:
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
            for f in leg.order.fills
        ]
    )


def trade_rows(
    outcome: TradeOutcome,
    *,
    trade_id: str,
    session_id: str,
    run_id: str,
    cycle_no: int,
    fee_config: FeeConfig,
) -> tuple[TradeRow, list[TradeOrderRow]]:
    plan = outcome.plan
    opportunity = plan.opportunity
    attribution = outcome.attribution
    flat = outcome.status == "flat"
    trade = TradeRow(
        trade_id=trade_id,
        session_id=session_id,
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
        netted_cash=outcome.netted_cash.raw,
        balance_change=None if outcome.balance_change is None else outcome.balance_change.raw,
    )
    orders: list[TradeOrderRow] = []
    for seq, (order, leg) in enumerate(zip(plan.orders, plan.basket.legs, strict=True)):
        orders.append(
            TradeOrderRow(
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
    for phase, placed_orders in (("entry", outcome.entry), ("hedge", outcome.hedge)):
        for seq, placed in enumerate(placed_orders):
            order = placed.order
            orders.append(
                TradeOrderRow(
                    trade_id,
                    phase,
                    seq,
                    order.ticker,
                    order.side.value,
                    order.limit.raw,
                    order.qty.raw,
                    placed.filled.raw,
                    placed.cash_out.raw,
                    placed.fees.raw,
                    _fills_json(placed.leg_fill()),
                    client_order_id=placed.client_order_id,
                    order_id=placed.order_id,
                    model_fees=placed.model_fees(plan.fees, fee_config).raw,
                    error=placed.error or None,
                    response=placed.response_json() or None,
                )
            )
    return trade, orders
