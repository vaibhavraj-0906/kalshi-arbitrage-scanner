"""Top-of-book screens: cheap necessary conditions, run over every eligible event.

Depth only makes prices worse and fees only make costs higher, so an arbitrage that does not
exist at the best quotes before fees cannot exist at all. The screens check exactly that, in
integers, for the basket families that matter:

* NO on pairwise-exclusive markets -- profitable only if their YES bids sum above $1;
* YES on markets covering every atom -- profitable only if their YES asks sum below $1;
* YES on market A and NO on market B whose YES-set A contains -- only if bid(B) > ask(A).

For interval events every YES-set is a contiguous run of atoms, so the best exclusive set is
weighted interval scheduling and the cheapest cover is a shortest path. A screen hit is only
a *candidate*: it is confirmed, sized and priced against full books before anything is shown.
"""

from __future__ import annotations

import heapq
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import permutations

from karb.core.fixed import PRICE_SCALE, Price
from karb.market.model import Quote
from karb.structure.classify import EventStructure, StructureKind, Tier
from karb.structure.intervals import AtomKind, OutcomeSpace

__all__ = ["ScreenHit", "screen_event"]


@dataclass(frozen=True, slots=True)
class ScreenHit:
    tier: Tier
    rule: str
    gross_edge: Price
    """Best-case edge per contract before depth and fees."""
    detail: str


def screen_event(
    structure: EventStructure,
    quotes: Mapping[str, Quote],
    tradeable: frozenset[str],
) -> list[ScreenHit]:
    hits: list[ScreenHit] = []
    for tier, space in structure.spaces.items():
        hits.extend(_screen_space(structure.kind, tier, space, quotes, tradeable))
    return hits


def _screen_space(
    kind: StructureKind,
    tier: Tier,
    space: OutcomeSpace,
    quotes: Mapping[str, Quote],
    tradeable: frozenset[str],
) -> list[ScreenHit]:
    bids: dict[str, int] = {}
    asks: dict[str, int] = {}
    for ticker in space.tickers:
        quote = quotes.get(ticker)
        if ticker not in tradeable or quote is None:
            continue
        if quote.yes_bid is not None:
            bids[ticker] = quote.yes_bid.raw
        if quote.yes_ask is not None:
            asks[ticker] = quote.yes_ask.raw

    hits: list[ScreenHit] = []
    if kind is StructureKind.CATEGORICAL:
        bid_sum = sum(bids.values())
        if bid_sum > PRICE_SCALE:
            hits.append(_hit(tier, "bids over $1", bid_sum - PRICE_SCALE, f"{len(bids)} bids"))
        has_residual = any(atom.kind is AtomKind.RESIDUAL for atom in space.atoms)
        if not has_residual and len(asks) == len(space.tickers):
            ask_sum = sum(asks.values())
            if ask_sum < PRICE_SCALE:
                hits.append(_hit(tier, "asks under $1", PRICE_SCALE - ask_sum, f"{len(asks)} asks"))
        return hits

    runs = {ticker: space.run(ticker) for ticker in space.tickers}
    bid_runs = [(r[0], r[1], bids[t]) for t, r in runs.items() if r is not None and t in bids]
    ask_runs = [(r[0], r[1], asks[t]) for t, r in runs.items() if r is not None and t in asks]

    best_exclusive = _max_exclusive_weight(bid_runs)
    if best_exclusive > PRICE_SCALE:
        hits.append(_hit(tier, "exclusive bids over $1", best_exclusive - PRICE_SCALE, ""))
    cheapest_cover = _min_cover_cost(space.size, ask_runs)
    if cheapest_cover is not None and cheapest_cover < PRICE_SCALE:
        hits.append(_hit(tier, "covering asks under $1", PRICE_SCALE - cheapest_cover, ""))

    for wide, narrow in permutations(runs, 2):
        wide_run, narrow_run = runs[wide], runs[narrow]
        if wide_run is None or narrow_run is None or wide not in asks or narrow not in bids:
            continue
        contains = wide_run[0] <= narrow_run[0] and narrow_run[1] <= wide_run[1]
        if contains and bids[narrow] > asks[wide]:
            hits.append(
                _hit(
                    tier,
                    "bid above containing ask",
                    bids[narrow] - asks[wide],
                    f"bid({narrow}) > ask({wide})",
                )
            )
    return hits


def _hit(tier: Tier, rule: str, edge: int, detail: str) -> ScreenHit:
    return ScreenHit(tier, rule, Price(min(edge, PRICE_SCALE)), detail)


def _max_exclusive_weight(runs: list[tuple[int, int, int]]) -> int:
    """Largest total weight of pairwise non-overlapping runs (weighted interval scheduling)."""
    ordered = sorted(runs, key=lambda run: run[1])
    ends = [run[1] for run in ordered]
    best = [0] * (len(ordered) + 1)
    for index, (first, _last, weight) in enumerate(ordered, start=1):
        compatible = bisect_left(ends, first, 0, index - 1)
        best[index] = max(best[index - 1], best[compatible] + weight)
    return best[-1]


def _min_cover_cost(n_atoms: int, runs: list[tuple[int, int, int]]) -> int | None:
    """Cheapest set of runs covering every atom, or ``None`` if no cover exists.

    Shortest path over positions 0..n: position p means atoms [0, p) are covered. A run
    [first, last] is an edge first -> last + 1; stepping back from p to p - 1 is free, because
    covering more than needed is allowed.
    """
    starting: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for first, last, cost in runs:
        starting[first].append((last + 1, cost))
    distance: dict[int, int] = {0: 0}
    frontier = [(0, 0)]
    while frontier:
        cost, position = heapq.heappop(frontier)
        if cost > distance.get(position, cost):
            continue
        if position == n_atoms:
            return cost
        steps = [(position - 1, 0)] if position > 0 else []
        steps.extend(starting.get(position, ()))
        for target, step_cost in steps:
            candidate = cost + step_cost
            if candidate < distance.get(target, candidate + 1):
                distance[target] = candidate
                heapq.heappush(frontier, (candidate, target))
    return None
