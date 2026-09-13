"""From a snapshot of an event's books to verified opportunities.

For each outcome-space tier, weakest assumption first:

1. solve the basket LP over every tradeable, uncrossed book;
2. turn the float proposal into whole-contract candidates;
3. verify every candidate exactly and keep the best guaranteed P&L;
4. report it if it clears the minimum profit.

The first tier that yields an opportunity wins: a basket found under LOGICAL assumptions also
exists under STRUCTURAL ones, and reporting both would count one opportunity twice.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations

from karb.arb.lp import LegQuotes, LpSolution, solve_basket
from karb.arb.opportunity import ArbKind, Opportunity, VerifiedBasket, opportunity_id
from karb.arb.verify import verify_basket
from karb.core.fixed import Cash, Qty
from karb.market.book import OrderBook, Side
from karb.market.fees import FeeConfig
from karb.structure.classify import EventStructure, Tier
from karb.structure.intervals import OutcomeSpace

__all__ = ["DetectConfig", "Detection", "EventSnapshot", "classify_basket", "detect"]

_SCALES = (1.0, 0.75, 0.5, 0.25, 0.1)
DEFAULT_MIN_PROFIT = Cash(10_000)


@dataclass(frozen=True, slots=True)
class DetectConfig:
    fees: FeeConfig = field(default_factory=FeeConfig)
    min_profit: Cash = DEFAULT_MIN_PROFIT
    """Smallest guaranteed P&L, after fees and rounding, worth reporting. Default $0.01."""
    max_levels: int = 10
    """Book depth per side considered by the LP."""
    max_contracts_per_leg: int | None = 5_000
    max_cost: Cash | None = None


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    structure: EventStructure
    books: Mapping[str, OrderBook]
    tradeable: frozenset[str]
    observed_at: datetime
    skew_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Detection:
    opportunities: tuple[Opportunity, ...]
    lp: Mapping[Tier, LpSolution]
    integrity: tuple[str, ...]
    """Books that were missing or crossed; those markets were treated as untradeable."""


def detect(snapshot: EventSnapshot, config: DetectConfig) -> Detection:
    structure = snapshot.structure
    integrity: list[str] = []
    usable: dict[str, OrderBook] = {}
    for ticker in sorted(snapshot.tradeable):
        book = snapshot.books.get(ticker)
        if book is None:
            integrity.append(f"{ticker}: no order book in snapshot")
        elif book.is_crossed:
            integrity.append(f"{ticker}: crossed book")
        else:
            usable[ticker] = book

    rate = float(structure.fees.taker_rate(config.fees))
    solutions: dict[Tier, LpSolution] = {}
    for tier, space in structure.spaces.items():
        solution = solve_basket(
            space.size,
            [
                LegQuotes(
                    ticker,
                    space.yes_atoms[ticker],
                    book.asks(Side.YES)[: config.max_levels],
                    book.asks(Side.NO)[: config.max_levels],
                )
                for ticker, book in usable.items()
            ],
            rate,
            max_contracts_per_leg=config.max_contracts_per_leg,
            max_cost=None if config.max_cost is None else config.max_cost.to_float(),
        )
        solutions[tier] = solution
        if not solution.found:
            continue
        basket = _best_verified(space, usable, solution, structure, config)
        if basket is not None and basket.guaranteed_pnl >= config.min_profit:
            opportunity = _opportunity(snapshot, tier, space, basket)
            return Detection((opportunity,), solutions, tuple(integrity))
    return Detection((), solutions, tuple(integrity))


def whole_contract_candidates(solution: LpSolution) -> list[dict[tuple[str, Side], Qty]]:
    """Integer baskets near the LP's proposal. The verifier decides which, if any, is good.

    Flooring can unbalance a hedge, so several scalings are tried, plus the smallest possible
    basket of one contract per leg.
    """
    candidates: list[dict[tuple[str, Side], Qty]] = []

    def add(candidate: dict[tuple[str, Side], Qty]) -> None:
        trimmed = {key: qty for key, qty in candidate.items() if not qty.is_zero}
        if trimmed and trimmed not in candidates:
            candidates.append(trimmed)

    for scale in _SCALES:
        add(
            {
                key: Qty.contracts(math.floor(v * scale + 1e-6))
                for key, v in solution.quantities.items()
            }
        )
    smallest = min(solution.quantities.values())
    add({key: Qty.contracts(math.floor(smallest + 1e-6)) for key in solution.quantities})
    add({key: Qty.contracts(1) for key in solution.quantities})
    return candidates


def _best_verified(
    space: OutcomeSpace,
    books: Mapping[str, OrderBook],
    solution: LpSolution,
    structure: EventStructure,
    config: DetectConfig,
) -> VerifiedBasket | None:
    best: VerifiedBasket | None = None
    for candidate in whole_contract_candidates(solution):
        basket = verify_basket(space, books, candidate, structure.fees, config.fees)
        if not basket.legs:
            continue
        if (
            best is None
            or basket.guaranteed_pnl > best.guaranteed_pnl
            or (basket.guaranteed_pnl == best.guaranteed_pnl and basket.cost < best.cost)
        ):
            best = basket
    return best


def classify_basket(
    basket: VerifiedBasket, space: OutcomeSpace, mutually_exclusive: bool
) -> ArbKind:
    legs = basket.legs
    sides = {leg.side for leg in legs}
    sets = [space.yes_atoms[leg.ticker] for leg in legs]
    if len(legs) >= 2 and sides == {Side.NO} and all(not a & b for a, b in combinations(sets, 2)):
        return ArbKind.OVERROUND if len(legs) > 2 or mutually_exclusive else ArbKind.DISJOINT
    if (
        len(legs) >= 2
        and sides == {Side.YES}
        and frozenset().union(*sets) == frozenset(range(space.size))
    ):
        return ArbKind.UNDERROUND if len(legs) > 2 else ArbKind.COVER
    if len(legs) == 2 and sides == {Side.YES, Side.NO}:
        yes_leg = next(leg for leg in legs if leg.side is Side.YES)
        no_leg = next(leg for leg in legs if leg.side is Side.NO)
        if space.yes_atoms[no_leg.ticker] <= space.yes_atoms[yes_leg.ticker]:
            return ArbKind.MONOTONE
    return ArbKind.COMBO


def _opportunity(
    snapshot: EventSnapshot, tier: Tier, space: OutcomeSpace, basket: VerifiedBasket
) -> Opportunity:
    event = snapshot.structure.event
    tickers = {leg.ticker for leg in basket.legs}
    expirations = [
        market.latest_expiration_time
        for market in event.markets
        if market.ticker in tickers and market.latest_expiration_time is not None
    ]
    return Opportunity(
        id=opportunity_id(
            event.event_ticker, tier, ((leg.ticker, leg.side) for leg in basket.legs)
        ),
        event_ticker=event.event_ticker,
        series_ticker=event.series_ticker,
        title=event.title,
        kind=classify_basket(basket, space, event.mutually_exclusive),
        tier=tier,
        basket=basket,
        space=space,
        observed_at=snapshot.observed_at,
        snapshot_skew=snapshot.skew_seconds,
        expires=max(expirations) if expirations else None,
        collateral_return_type=event.collateral_return_type,
    )
