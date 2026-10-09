"""The basket LP: the most profitable guaranteed payoff the books allow (ADR-0003).

For every tradeable market i, side s and ask level l:

    x[i,s,l] in [0, size]      contracts bought at that level
    y[i,s]   = sum_l x[i,s,l]  contracts of that side in the basket
    t                           the payoff the basket guarantees

    maximise   t - sum c[i,s,l] * x[i,s,l]
    subject to t <= base[w] + sum of y[i,s] over the (i, s) that pay in atom w,   for every atom w

with ``c = p + rate * p * (1 - p)``: price plus the pre-rounding taker fee, which is linear in
quantity at a fixed price. ``base`` is the payoff of a position already held -- zero when looking
for a fresh basket. A positive optimum with no position means no probability distribution over
the atoms is consistent with the fee-adjusted asks -- the finite-state fundamental theorem of
asset pricing -- and the optimal x is the basket that monetises the inconsistency. With a
position, the same programme finds the trades that most improve its worst case: completing a
half-filled basket, or unwinding it (ADR-0008).

The LP works in floats and only *proposes*. Every reported number comes from
``karb.arb.verify``, which re-prices the proposal exactly, in every atom.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_array

from karb.market.book import Level, Side

__all__ = ["PROFIT_TOLERANCE", "LegQuotes", "LpSolution", "solve_basket"]

PROFIT_TOLERANCE: Final = 1e-7
"""LP profits below this (dollars) are solver noise, not opportunities."""


@dataclass(frozen=True, slots=True)
class LegQuotes:
    ticker: str
    yes_atoms: frozenset[int]
    yes_asks: tuple[Level, ...]
    no_asks: tuple[Level, ...]


@dataclass(frozen=True, slots=True)
class LpSolution:
    status: str
    profit: float
    """Guaranteed payoff (base included) minus the cost of new trades, in dollars, before fee
    rounding. With no base position this is the basket's guaranteed profit."""
    quantities: Mapping[tuple[str, Side], float]
    """Contracts per (market, side), for sides the basket actually buys."""

    @property
    def found(self) -> bool:
        return self.profit > PROFIT_TOLERANCE and bool(self.quantities)


def solve_basket(
    n_atoms: int,
    legs: Sequence[LegQuotes],
    fee_rate: float,
    *,
    max_contracts_per_leg: float | None = None,
    max_cost: float | None = None,
    base_payoff: Sequence[float] | None = None,
) -> LpSolution:
    if base_payoff is not None and len(base_payoff) != n_atoms:
        raise ValueError(f"base payoff has {len(base_payoff)} atoms, expected {n_atoms}")
    base_worst = min(base_payoff) if base_payoff else 0.0
    groups: list[tuple[str, Side, frozenset[int]]] = []
    costs: list[float] = []
    caps: list[float] = []
    owner: list[int] = []
    for leg in legs:
        for side, asks in ((Side.YES, leg.yes_asks), (Side.NO, leg.no_asks)):
            if not asks:
                continue
            group = len(groups)
            groups.append((leg.ticker, side, leg.yes_atoms))
            for level in asks:
                price = level.price.to_float()
                costs.append(price + fee_rate * price * (1.0 - price))
                caps.append(level.qty.to_float())
                owner.append(group)
    if not groups or n_atoms == 0:
        return LpSolution("nothing to buy", base_worst, {})

    n_levels, n_groups = len(costs), len(groups)
    t_col = n_levels + n_groups
    n_cols = t_col + 1

    objective = np.zeros(n_cols)
    objective[:n_levels] = costs
    objective[t_col] = -1.0

    # y[g] - sum of g's level variables = 0
    eq_rows = np.concatenate([np.arange(n_groups), np.asarray(owner)])
    eq_cols = np.concatenate([n_levels + np.arange(n_groups), np.arange(n_levels)])
    eq_vals = np.concatenate([np.ones(n_groups), -np.ones(n_levels)])
    a_eq = coo_array((eq_vals, (eq_rows, eq_cols)), shape=(n_groups, n_cols))

    # t - sum of the y that pay in atom w <= base[w]
    pays = np.zeros((n_groups, n_atoms), dtype=bool)
    for group, (_ticker, side, yes_atoms) in enumerate(groups):
        inside = np.zeros(n_atoms, dtype=bool)
        if yes_atoms:
            inside[np.fromiter(yes_atoms, dtype=np.intp)] = True
        pays[group] = inside if side is Side.YES else ~inside
    pay_group, pay_atom = np.nonzero(pays)
    ub_rows = np.concatenate([np.arange(n_atoms), pay_atom])
    ub_cols = np.concatenate([np.full(n_atoms, t_col), n_levels + pay_group])
    ub_vals = np.concatenate([np.ones(n_atoms), -np.ones(len(pay_atom))])
    b_ub = np.zeros(n_atoms) if base_payoff is None else np.asarray(base_payoff, dtype=float)
    if max_cost is not None:
        ub_rows = np.concatenate([ub_rows, np.full(n_levels, n_atoms)])
        ub_cols = np.concatenate([ub_cols, np.arange(n_levels)])
        ub_vals = np.concatenate([ub_vals, np.asarray(costs)])
        b_ub = np.append(b_ub, max_cost)
    a_ub = coo_array((ub_vals, (ub_rows, ub_cols)), shape=(len(b_ub), n_cols))

    bounds = [
        *((0.0, cap) for cap in caps),
        *((0.0, max_contracts_per_leg) for _ in groups),
        (None, None),
    ]
    # scipy-stubs does not model sparse constraint matrices, which HiGHS accepts natively.
    result = linprog(  # type: ignore[call-overload]
        objective,
        A_ub=a_ub,
        b_ub=b_ub,
        A_eq=a_eq,
        b_eq=np.zeros(n_groups),
        bounds=bounds,
        method="highs",
    )
    if result.status != 0:
        return LpSolution(f"solver: {result.message}", base_worst, {})
    solution = np.asarray(result.x, dtype=float)
    quantities = {
        (groups[group][0], groups[group][1]): float(solution[n_levels + group])
        for group in range(n_groups)
        if solution[n_levels + group] > 1e-9
    }
    return LpSolution("optimal", -float(result.fun), quantities)
