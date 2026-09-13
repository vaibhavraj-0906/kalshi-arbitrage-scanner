"""Order books: Kalshi's two bid ladders, and the asks they imply.

Kalshi publishes bids only. A YES bid at p is an offer to sell NO at 1 - p, so the cheapest
way to buy YES is to take the best NO bid, and vice versa. Everything the scanner trades is
therefore a *buy* of one side from the other side's bids.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from karb.core.fixed import PRICE_SCALE, FixedPointError, Price, Qty

__all__ = ["BookIntegrityError", "Level", "OrderBook", "Side"]


class Side(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def other(self) -> Side:
        return Side.NO if self is Side.YES else Side.YES


class BookIntegrityError(ValueError):
    """An order book payload is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class Level:
    price: Price
    qty: Qty


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Both bid ladders for one market, each ordered best (highest price) first."""

    ticker: str
    yes_bids: tuple[Level, ...]
    no_bids: tuple[Level, ...]

    def __post_init__(self) -> None:
        for side, ladder in ((Side.YES, self.yes_bids), (Side.NO, self.no_bids)):
            for level in ladder:
                if level.qty.is_zero:
                    raise BookIntegrityError(f"{self.ticker}: empty {side} level at {level.price}")
            for better, worse in pairwise(ladder):
                if not worse.price < better.price:
                    raise BookIntegrityError(
                        f"{self.ticker}: {side} bids not strictly descending "
                        f"({better.price} then {worse.price})"
                    )

    @classmethod
    def from_wire(
        cls,
        ticker: str,
        yes_dollars: Sequence[tuple[str, str]] | None,
        no_dollars: Sequence[tuple[str, str]] | None,
    ) -> OrderBook:
        """Build from ``orderbook_fp`` arrays, which are ascending with the best bid last."""

        def ladder(levels: Sequence[tuple[str, str]] | None) -> tuple[Level, ...]:
            parsed = [Level(Price.parse(price), Qty.parse(count)) for price, count in levels or ()]
            return tuple(reversed(parsed))

        try:
            return cls(ticker, ladder(yes_dollars), ladder(no_dollars))
        except FixedPointError as exc:
            raise BookIntegrityError(f"{ticker}: {exc}") from exc

    def bids(self, side: Side) -> tuple[Level, ...]:
        return self.yes_bids if side is Side.YES else self.no_bids

    def asks(self, side: Side) -> tuple[Level, ...]:
        """Offers to sell ``side``, cheapest first: the other side's bids at 1 - p."""
        return tuple(Level(level.price.complement(), level.qty) for level in self.bids(side.other))

    def best_bid(self, side: Side) -> Level | None:
        ladder = self.bids(side)
        return ladder[0] if ladder else None

    def best_ask(self, side: Side) -> Level | None:
        best = self.best_bid(side.other)
        return None if best is None else Level(best.price.complement(), best.qty)

    @property
    def is_crossed(self) -> bool:
        """The best YES and NO bids sum to $1 or more.

        A live matching engine would already have traded those two orders against each other,
        so a crossed snapshot is a data error -- never an opportunity.
        """
        yes, no = self.best_bid(Side.YES), self.best_bid(Side.NO)
        return yes is not None and no is not None and yes.price.raw + no.price.raw >= PRICE_SCALE

    def sweep(self, side: Side, qty: Qty) -> tuple[tuple[Level, ...], Qty]:
        """Buy up to ``qty`` of ``side`` from the asks, cheapest first.

        Returns the fills and whatever quantity the book could not supply.
        """
        fills: list[Level] = []
        remaining = qty
        for level in self.asks(side):
            if remaining.is_zero:
                break
            take = min(level.qty, remaining)
            fills.append(Level(level.price, take))
            remaining = remaining - take
        return tuple(fills), remaining
