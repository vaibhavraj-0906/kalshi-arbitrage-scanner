"""Simulated taker execution (docs/decisions/ADR-0008).

Every order is an immediate-or-cancel limit buy. It takes whatever the book offers at or below its
limit, cheapest first, up to its quantity; the rest is cancelled.

A paper trader never moves the real market, so left alone it would take the same liquidity again
and again. ``Liquidity`` remembers every contract a trade has taken and keeps it taken -- in later
books of the same trade too. If a later snapshot still shows that size, it is assumed to be the
size we already bought, not fresh size we could buy again.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from karb.arb.opportunity import LegFill, VerifiedBasket
from karb.core.fixed import Cash, Price, Qty
from karb.market.book import Level, OrderBook, Side
from karb.market.fees import FeeConfig, FeeSchedule, taker_buy_cost

__all__ = [
    "Execution",
    "Liquidity",
    "Order",
    "execute",
    "plan_orders",
    "position_cost",
    "position_legs",
]


@dataclass(frozen=True, slots=True)
class Order:
    """A taker buy of ``qty`` contracts of ``side``, at prices no worse than ``limit``."""

    ticker: str
    side: Side
    limit: Price
    qty: Qty


@dataclass(frozen=True, slots=True)
class Execution:
    order: Order
    fill: LegFill | None
    """What actually traded, exactly priced; ``None`` when nothing did."""

    @property
    def filled(self) -> Qty:
        return Qty.ZERO if self.fill is None else self.fill.order.qty

    @property
    def cash_out(self) -> Cash:
        return Cash.ZERO if self.fill is None else self.fill.order.cash_out

    @property
    def fees(self) -> Cash:
        return Cash.ZERO if self.fill is None else self.fill.order.fees


def plan_orders(basket: VerifiedBasket) -> tuple[Order, ...]:
    """One order per leg, limited to the worst price the decision book needed."""
    return tuple(
        Order(leg.ticker, leg.side, max(fill.price for fill in leg.order.fills), leg.order.qty)
        for leg in basket.legs
    )


class Liquidity:
    """The asks still available to one trade: displayed size less what it already took."""

    __slots__ = ("_taken",)

    def __init__(self, taken: Mapping[tuple[str, Side, int], int] | None = None) -> None:
        self._taken: dict[tuple[str, Side, int], int] = dict(taken or {})

    def copy(self) -> Liquidity:
        return Liquidity(self._taken)

    def asks(self, book: OrderBook, side: Side) -> tuple[Level, ...]:
        available: list[Level] = []
        for level in book.asks(side):
            left = level.qty.raw - self._taken.get((book.ticker, side, level.price.raw), 0)
            if left > 0:
                available.append(Level(level.price, Qty(left)))
        return tuple(available)

    def take(self, book: OrderBook, side: Side, qty: Qty, limit: Price | None) -> tuple[Level, ...]:
        """Buy up to ``qty`` cheapest-first at or below ``limit``, and remember it."""
        fills: list[Level] = []
        remaining = qty.raw
        for level in self.asks(book, side):
            if remaining == 0 or (limit is not None and level.price > limit):
                break
            take = min(remaining, level.qty.raw)
            fills.append(Level(level.price, Qty(take)))
            key = (book.ticker, side, level.price.raw)
            self._taken[key] = self._taken.get(key, 0) + take
            remaining -= take
        return tuple(fills)


def execute(
    orders: Iterable[Order],
    books: Mapping[str, OrderBook],
    liquidity: Liquidity,
    fees: FeeSchedule,
    config: FeeConfig,
) -> tuple[Execution, ...]:
    """Fill ``orders`` in sequence against ``books``. Missing or crossed books fill nothing."""
    executions: list[Execution] = []
    for order in orders:
        book = books.get(order.ticker)
        if book is None or book.is_crossed:
            executions.append(Execution(order, None))
            continue
        fills = liquidity.take(book, order.side, order.qty, order.limit)
        fill = (
            LegFill(order.ticker, order.side, taker_buy_cost(fills, fees, config))
            if fills
            else None
        )
        executions.append(Execution(order, fill))
    return tuple(executions)


def position_legs(*phases: Sequence[Execution]) -> tuple[LegFill, ...]:
    return tuple(e.fill for phase in phases for e in phase if e.fill is not None)


def position_cost(legs: Iterable[LegFill]) -> Cash:
    return Cash.total(leg.order.cash_out for leg in legs)
