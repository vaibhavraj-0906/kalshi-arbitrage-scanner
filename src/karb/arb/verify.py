"""Exact verification: the only place a reported number comes from.

Given integer quantities per (market, side), sweep each book cheapest-first, price every fill
with Kalshi's fee rounding, and compute the basket's payout in every atom of the outcome
space. Nothing here uses floats, and nothing here trusts the LP that proposed the basket.
"""

from __future__ import annotations

from collections.abc import Mapping

from karb.arb.opportunity import LegFill, VerifiedBasket
from karb.core.fixed import Cash, Qty
from karb.market.book import OrderBook, Side
from karb.market.fees import FeeConfig, FeeSchedule, taker_buy_cost
from karb.structure.intervals import OutcomeSpace

__all__ = ["payoff_by_atom", "verify_basket"]


def payoff_by_atom(space: OutcomeSpace, legs: tuple[LegFill, ...]) -> tuple[Cash, ...]:
    """Total payout of ``legs`` in each atom of ``space``."""
    # Every NO pays everywhere except its own YES atoms; start from that and correct.
    base = sum(leg.order.qty.payout().raw for leg in legs if leg.side is Side.NO)
    payoffs = [base] * space.size
    for leg in legs:
        payout = leg.order.qty.payout().raw
        delta = payout if leg.side is Side.YES else -payout
        for atom in space.yes_atoms[leg.ticker]:
            payoffs[atom] += delta
    return tuple(Cash(raw) for raw in payoffs)


def verify_basket(
    space: OutcomeSpace,
    books: Mapping[str, OrderBook],
    quantities: Mapping[tuple[str, Side], Qty],
    fees: FeeSchedule,
    config: FeeConfig,
) -> VerifiedBasket:
    """Price ``quantities`` against ``books`` exactly.

    A quantity larger than the book can supply is filled only as far as the book goes: the
    basket is verified as it could actually be executed, never as it was requested.
    """
    legs: list[LegFill] = []
    for (ticker, side), qty in sorted(quantities.items()):
        if qty.is_zero:
            continue
        fills, _unfilled = books[ticker].sweep(side, qty)
        if not fills:
            continue
        legs.append(LegFill(ticker, side, taker_buy_cost(fills, fees, config)))
    frozen = tuple(legs)
    return VerifiedBasket(frozen, payoff_by_atom(space, frozen))
