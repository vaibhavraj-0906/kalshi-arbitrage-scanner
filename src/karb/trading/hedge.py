"""Repairing a half-filled basket (docs/decisions/ADR-0008).

When some legs fill and others do not, the position is no longer riskless. The repair is the same
optimisation that found the basket: the basket LP, started from the position's payoff in every
atom, finds the trades that most raise its worst case. That might complete the missing legs at a
worse price, unwind the filled ones by buying their other side, or a mix -- whichever the books
make cheapest. Doing nothing is always a candidate, so a hedge is only sent when it strictly
improves the exact, fee-rounded worst case.

The hedge only trades markets already in the position. Searching the whole event could turn up a
fresh, unrelated arbitrage, which would then be booked as "hedging" in the attribution.

The books are fetched after the entry orders have executed, so they already lack the size the
entry took. Nothing has to be remembered between the two.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from karb.arb.detect import whole_contract_candidates
from karb.arb.lp import PROFIT_TOLERANCE, LegQuotes, solve_basket
from karb.arb.opportunity import LegFill
from karb.arb.verify import payoff_by_atom
from karb.core.fixed import Cash
from karb.market.book import OrderBook, Side
from karb.market.fees import FeeConfig, FeeSchedule, taker_buy_cost
from karb.structure.intervals import OutcomeSpace
from karb.trading.orders import Order, position_cost, walk

__all__ = ["decide_hedge", "worst_and_best"]


def worst_and_best(space: OutcomeSpace, legs: Sequence[LegFill]) -> tuple[Cash, Cash]:
    """A position's P&L in its worst and best atoms, exactly. Zero for no position."""
    if not legs:
        return Cash.ZERO, Cash.ZERO
    payoffs = payoff_by_atom(space, tuple(legs))
    cost = position_cost(legs)
    return min(payoffs) - cost, max(payoffs) - cost


def decide_hedge(
    space: OutcomeSpace,
    books: Mapping[str, OrderBook],
    position: Sequence[LegFill],
    tradeable: frozenset[str],
    fees: FeeSchedule,
    config: FeeConfig,
    *,
    max_levels: int,
) -> tuple[Order, ...]:
    """The orders that most improve ``position``'s worst case at ``books``, or none."""
    if not position:
        return ()
    held = sorted({leg.ticker for leg in position})
    tickers = [t for t in held if t in tradeable and t in books and not books[t].is_crossed]
    if not tickers:
        return ()
    payoffs = payoff_by_atom(space, tuple(position))
    base = [payoff.to_float() for payoff in payoffs]
    largest = max(leg.order.qty for leg in position)
    solution = solve_basket(
        space.size,
        [
            LegQuotes(
                ticker,
                space.yes_atoms[ticker],
                books[ticker].asks(Side.YES)[:max_levels],
                books[ticker].asks(Side.NO)[:max_levels],
            )
            for ticker in tickers
        ],
        float(fees.taker_rate(config)),
        max_contracts_per_leg=largest.to_float(),
        base_payoff=base,
    )
    if not solution.quantities or solution.profit <= min(base) + PROFIT_TOLERANCE:
        return ()

    best_value, _ = worst_and_best(space, position)
    best_orders: tuple[Order, ...] = ()
    for candidate in whole_contract_candidates(solution):
        orders: list[Order] = []
        added: list[LegFill] = []
        for (ticker, side), qty in sorted(candidate.items()):
            fills = walk(books[ticker], side, qty, None)
            if not fills:
                continue
            cost = taker_buy_cost(fills, fees, config)
            orders.append(Order(ticker, side, max(fill.price for fill in fills), cost.qty))
            added.append(LegFill(ticker, side, cost))
        if not added:
            continue
        value, _ = worst_and_best(space, (*position, *added))
        if value > best_value:
            best_value, best_orders = value, tuple(orders)
    return best_orders
