"""Opportunities: exactly verified baskets, and how long each has been seen."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from karb.core.fixed import Cash
from karb.market.book import Side
from karb.market.fees import OrderCost
from karb.structure.classify import Tier
from karb.structure.intervals import OutcomeSpace

__all__ = [
    "ArbKind",
    "LegFill",
    "Opportunity",
    "OpportunityTracker",
    "Sighting",
    "VerifiedBasket",
    "opportunity_id",
]

_NETTED_COLLATERAL: Final = frozenset({"MECNET", "DIRECNET"})
_SECONDS_PER_YEAR: Final = 365.25 * 24 * 3600


class ArbKind(StrEnum):
    """The shape of a basket. Descriptive only: every kind is verified the same way."""

    OVERROUND = "OVERROUND"
    """NO on mutually exclusive markets whose YES bids sum above $1."""
    UNDERROUND = "UNDERROUND"
    """YES on markets covering every outcome, whose asks sum below $1."""
    MONOTONE = "MONOTONE"
    """YES on a market, NO on one whose YES-set it contains, priced above it."""
    DISJOINT = "DISJOINT"
    """NO on two markets that cannot both resolve YES."""
    COVER = "COVER"
    """YES on two markets that cannot both resolve NO."""
    COMBO = "COMBO"
    """Any other basket the LP found."""
    EXERCISE = "EXERCISE"
    """A complete set bought on purpose to exercise trading (``karb trade --exercise``): it
    pays exactly $1 a set and is usually a small known loss. Never reported as an opportunity."""


@dataclass(frozen=True, slots=True)
class LegFill:
    ticker: str
    side: Side
    order: OrderCost


@dataclass(frozen=True, slots=True)
class VerifiedBasket:
    """A basket priced exactly: every fill, every fee, the payoff in every atom."""

    legs: tuple[LegFill, ...]
    payoffs: tuple[Cash, ...]
    """Total payout in each atom of the outcome space it was verified against."""

    @property
    def cost(self) -> Cash:
        return Cash.total(leg.order.cash_out for leg in self.legs)

    @property
    def fees(self) -> Cash:
        return Cash.total(leg.order.fees for leg in self.legs)

    @property
    def min_payoff(self) -> Cash:
        return min(self.payoffs)

    @property
    def max_payoff(self) -> Cash:
        return max(self.payoffs)

    @property
    def guaranteed_pnl(self) -> Cash:
        """What the basket makes in its worst atom, after every fee and rounding charge."""
        return self.min_payoff - self.cost

    @property
    def best_pnl(self) -> Cash:
        return self.max_payoff - self.cost


def opportunity_id(event_ticker: str, tier: Tier, legs: Iterable[tuple[str, Side]]) -> str:
    """Stable across scans for the same event, tier and set of (market, side) legs."""
    key = ",".join(sorted(f"{ticker}:{side}" for ticker, side in legs))
    return hashlib.sha1(f"{event_ticker}|{tier}|{key}".encode()).hexdigest()[:10]


@dataclass(frozen=True, slots=True)
class Opportunity:
    id: str
    event_ticker: str
    series_ticker: str
    title: str
    kind: ArbKind
    tier: Tier
    basket: VerifiedBasket
    space: OutcomeSpace
    observed_at: datetime
    snapshot_skew: float
    """Seconds between the first request and last response of the books behind it."""
    expires: datetime | None
    """The latest expiration among the basket's markets: when the capital comes back."""
    collateral_return_type: str

    @property
    def guaranteed_pnl(self) -> Cash:
        return self.basket.guaranteed_pnl

    @property
    def cost(self) -> Cash:
        return self.basket.cost

    @property
    def edge(self) -> float:
        """Guaranteed P&L per dollar of gross capital."""
        cost = self.cost.raw
        return self.guaranteed_pnl.raw / cost if cost > 0 else float("inf")

    @property
    def netted_capital(self) -> Cash | None:
        """Capital locked if collateral return is enabled: cost less the guaranteed payout.

        Shown as a sensitivity only. Collateral return is off by default, locked per event at
        the first order, and can block selling (docs/decisions/ADR-0005).
        """
        if self.collateral_return_type not in _NETTED_COLLATERAL:
            return None
        return max(Cash.ZERO, self.cost - self.basket.min_payoff)

    @property
    def apr(self) -> float | None:
        """Simple annualised edge on gross capital, to the latest expiration."""
        if self.expires is None:
            return None
        years = (self.expires - self.observed_at).total_seconds() / _SECONDS_PER_YEAR
        return self.edge / years if years > 0 else None


@dataclass
class Sighting:
    opportunity: Opportunity
    first_seen: datetime
    last_seen: datetime
    consecutive: int = 1
    best_pnl: Cash = field(default=Cash.ZERO)

    @property
    def age_seconds(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()


class OpportunityTracker:
    """Which opportunities are live, since when, and for how many consecutive snapshots."""

    def __init__(self, confirmations: int = 2) -> None:
        if confirmations < 1:
            raise ValueError("confirmations must be at least 1")
        self.confirmations = confirmations
        self._live: dict[str, Sighting] = {}
        self.ended: list[Sighting] = []

    def observe(self, event_ticker: str, found: Sequence[Opportunity], at: datetime) -> None:
        """Record one confirmation pass over ``event_ticker``.

        Anything previously live for the event and not found again has ended.
        """
        stale = {oid for oid, s in self._live.items() if s.opportunity.event_ticker == event_ticker}
        for opportunity in found:
            sighting = self._live.get(opportunity.id)
            if sighting is None:
                self._live[opportunity.id] = Sighting(
                    opportunity, at, at, best_pnl=opportunity.guaranteed_pnl
                )
            else:
                sighting.opportunity = opportunity
                sighting.last_seen = at
                sighting.consecutive += 1
                sighting.best_pnl = max(sighting.best_pnl, opportunity.guaranteed_pnl)
            stale.discard(opportunity.id)
        for oid in stale:
            self.ended.append(self._live.pop(oid))

    def is_confirmed(self, sighting: Sighting) -> bool:
        return sighting.consecutive >= self.confirmations

    def live(self) -> list[Sighting]:
        return sorted(self._live.values(), key=lambda s: s.opportunity.guaranteed_pnl, reverse=True)
