"""What an event can settle to: strike intervals and outcome atoms.

Every market in an event settles off the same outcome. This module splits the outcome space
into *atoms* -- regions on which every market's payoff is constant -- so a basket's payoff can
be checked in every state the event can end in, exhaustively (docs/decisions/ADR-0003).

Interval events
    Every market's YES-set is an interval of one settlement value. The atoms are each strike
    endpoint as a single point, each open gap between consecutive endpoints, and both tails.

Categorical events
    ``mutually_exclusive`` events whose markets are named outcomes. One atom per market, plus
    a residual "none of the listed" atom unless exhaustiveness is known. The residual atom is
    what stops "buy every YES" being mistaken for an arbitrage on an event that can end with
    none of the listed outcomes happening.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from karb.market.book import Side

__all__ = [
    "INTERVAL_STRIKE_TYPES",
    "Atom",
    "AtomKind",
    "Bound",
    "Interval",
    "OutcomeSpace",
    "StrikeType",
    "StructureError",
    "categorical_space",
    "interval_space",
    "settlement_grid",
    "structural_space",
    "yes_interval",
]


class StructureError(ValueError):
    """Strike data that cannot be turned into a well-defined outcome space."""


class StrikeType(StrEnum):
    GREATER = "greater"
    GREATER_OR_EQUAL = "greater_or_equal"
    LESS = "less"
    LESS_OR_EQUAL = "less_or_equal"
    BETWEEN = "between"
    FUNCTIONAL = "functional"
    CUSTOM = "custom"
    STRUCTURED = "structured"


INTERVAL_STRIKE_TYPES: Final = frozenset(
    {
        StrikeType.GREATER,
        StrikeType.GREATER_OR_EQUAL,
        StrikeType.LESS,
        StrikeType.LESS_OR_EQUAL,
        StrikeType.BETWEEN,
    }
)


@dataclass(frozen=True, slots=True)
class Bound:
    value: Decimal
    closed: bool


@dataclass(frozen=True, slots=True)
class Interval:
    """A YES-set: the settlement values that resolve a market YES. ``None`` is unbounded."""

    lower: Bound | None
    upper: Bound | None

    def __post_init__(self) -> None:
        lower, upper = self.lower, self.upper
        if lower is None or upper is None:
            return
        if lower.value > upper.value:
            raise StructureError(f"empty interval: lower {lower.value} above upper {upper.value}")
        if lower.value == upper.value and not (lower.closed and upper.closed):
            raise StructureError(f"empty interval at {lower.value}")

    def contains(self, value: Decimal) -> bool:
        lower, upper = self.lower, self.upper
        if lower is not None and (
            value < lower.value or (value == lower.value and not lower.closed)
        ):
            return False
        return not (
            upper is not None
            and (value > upper.value or (value == upper.value and not upper.closed))
        )

    @property
    def endpoints(self) -> tuple[Decimal, ...]:
        return tuple(bound.value for bound in (self.lower, self.upper) if bound is not None)

    def __str__(self) -> str:
        lower, upper = self.lower, self.upper
        left = "(-inf" if lower is None else ("[" if lower.closed else "(") + str(lower.value)
        right = "+inf)" if upper is None else str(upper.value) + ("]" if upper.closed else ")")
        return f"{left}, {right}"


def yes_interval(strike_type: str | None, floor: Decimal | None, cap: Decimal | None) -> Interval:
    """The YES-set that Kalshi's strike fields describe.

    =================  ===================
    ``greater``        X >  floor_strike
    ``greater_or_equal``  X >= floor_strike
    ``less``           X <  cap_strike
    ``less_or_equal``  X <= cap_strike
    ``between``        floor_strike <= X <= cap_strike
    =================  ===================

    Strictness comes from the strike type. The live S&P range ladder confirms the reading:
    ``less`` with cap 7225 is titled "7,224.9999 or below", ``between`` 7225..7249.9999 is
    "7,225 to 7,249.9999", and ``greater`` with floor 7924.9999 is "7,925 or above".
    """
    if strike_type is None:
        raise StructureError("market has no strike type")
    try:
        kind = StrikeType(strike_type)
    except ValueError:
        raise StructureError(f"unknown strike type {strike_type!r}") from None
    if kind not in INTERVAL_STRIKE_TYPES:
        raise StructureError(f"strike type {strike_type!r} has no interval semantics")

    def required(value: Decimal | None, name: str) -> Decimal:
        if value is None or not value.is_finite():
            raise StructureError(f"{strike_type} strike needs a finite {name}, got {value}")
        return value

    if kind is StrikeType.GREATER:
        return Interval(Bound(required(floor, "floor_strike"), closed=False), None)
    if kind is StrikeType.GREATER_OR_EQUAL:
        return Interval(Bound(required(floor, "floor_strike"), closed=True), None)
    if kind is StrikeType.LESS:
        return Interval(None, Bound(required(cap, "cap_strike"), closed=False))
    if kind is StrikeType.LESS_OR_EQUAL:
        return Interval(None, Bound(required(cap, "cap_strike"), closed=True))
    return Interval(
        Bound(required(floor, "floor_strike"), closed=True),
        Bound(required(cap, "cap_strike"), closed=True),
    )


class AtomKind(StrEnum):
    LOWER_TAIL = "lower_tail"
    POINT = "point"
    GAP = "gap"
    UPPER_TAIL = "upper_tail"
    OUTCOME = "outcome"
    RESIDUAL = "residual"


@dataclass(frozen=True, slots=True)
class Atom:
    kind: AtomKind
    label: str
    low: Decimal | None = None
    """``POINT``: the value. ``GAP``/``UPPER_TAIL``: the excluded left end."""
    high: Decimal | None = None
    """``POINT``: the value. ``GAP``/``LOWER_TAIL``: the excluded right end."""

    @property
    def width(self) -> Decimal | None:
        if self.kind is AtomKind.GAP and self.low is not None and self.high is not None:
            return self.high - self.low
        return None


@dataclass(frozen=True, slots=True)
class OutcomeSpace:
    """Atoms, and for each market the atoms in which its YES pays $1."""

    atoms: tuple[Atom, ...]
    yes_atoms: Mapping[str, frozenset[int]]
    epsilon: Decimal | None = None
    """The settlement grid assumed when sub-grid gaps were dropped; ``None`` if none were."""

    def __post_init__(self) -> None:
        for ticker, atoms in self.yes_atoms.items():
            if any(not 0 <= index < len(self.atoms) for index in atoms):
                raise StructureError(f"{ticker}: atom index out of range")

    @property
    def size(self) -> int:
        return len(self.atoms)

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(self.yes_atoms)

    def pays(self, ticker: str, side: Side, atom: int) -> bool:
        inside = atom in self.yes_atoms[ticker]
        return inside if side is Side.YES else not inside

    def coverage(self) -> tuple[int, ...]:
        """How many markets' YES pays in each atom."""
        counts = [0] * self.size
        for atoms in self.yes_atoms.values():
            for index in atoms:
                counts[index] += 1
        return tuple(counts)

    def holes(self) -> tuple[int, ...]:
        return tuple(index for index, count in enumerate(self.coverage()) if count == 0)

    def overlaps(self) -> tuple[int, ...]:
        return tuple(index for index, count in enumerate(self.coverage()) if count > 1)

    @property
    def is_partition(self) -> bool:
        """Exactly one market's YES pays in every atom."""
        return all(count == 1 for count in self.coverage())

    def run(self, ticker: str) -> tuple[int, int] | None:
        """``(first, last)`` if the market's YES atoms are contiguous, else ``None``."""
        atoms = self.yes_atoms[ticker]
        if not atoms:
            return None
        first, last = min(atoms), max(atoms)
        return (first, last) if last - first + 1 == len(atoms) else None


def _representative(atom: Atom) -> Decimal:
    """A settlement value inside the atom. Every market's payoff is constant on the atom."""
    if atom.kind is AtomKind.POINT and atom.low is not None:
        return atom.low
    if atom.kind is AtomKind.GAP and atom.low is not None and atom.high is not None:
        return (atom.low + atom.high) / 2
    if atom.kind is AtomKind.LOWER_TAIL and atom.high is not None:
        return atom.high - 1
    if atom.kind is AtomKind.UPPER_TAIL and atom.low is not None:
        return atom.low + 1
    raise StructureError(f"atom {atom.label!r} has no representative value")


def interval_space(intervals: Mapping[str, Interval]) -> OutcomeSpace:
    """The atoms of a set of YES-intervals over one settlement value."""
    endpoints = sorted({value for interval in intervals.values() for value in interval.endpoints})
    if not endpoints:
        raise StructureError("no finite strike endpoints")
    atoms = [Atom(AtomKind.LOWER_TAIL, f"(-inf, {endpoints[0]})", high=endpoints[0])]
    for index, value in enumerate(endpoints):
        atoms.append(Atom(AtomKind.POINT, f"{{{value}}}", low=value, high=value))
        if index + 1 < len(endpoints):
            following = endpoints[index + 1]
            atoms.append(Atom(AtomKind.GAP, f"({value}, {following})", low=value, high=following))
    atoms.append(Atom(AtomKind.UPPER_TAIL, f"({endpoints[-1]}, +inf)", low=endpoints[-1]))

    representatives = [_representative(atom) for atom in atoms]
    yes_atoms = {
        ticker: frozenset(i for i, value in enumerate(representatives) if interval.contains(value))
        for ticker, interval in intervals.items()
    }
    return OutcomeSpace(tuple(atoms), yes_atoms)


def settlement_grid(values: Sequence[Decimal]) -> Decimal:
    """The coarsest decimal grid holding every value: ``10**-d`` for the most decimals used."""
    decimals = 0
    for value in values:
        exponent = value.normalize().as_tuple().exponent
        if isinstance(exponent, int):
            decimals = max(decimals, -exponent)
    return Decimal(1).scaleb(-decimals)


def structural_space(space: OutcomeSpace) -> OutcomeSpace | None:
    """The same space without the sub-grid holes a tiled ladder leaves by construction.

    Kalshi tiles brackets as [7225, 7249.9999], [7250, 7274.9999], ... Between each pair sits
    the open gap (7249.9999, 7250): no market covers it, and it holds no value on the 0.0001
    grid the strikes are written on. Dropping those gaps assumes the settlement value lives on
    that grid -- an inference from how the ladder was built, not a fact about the settlement
    source -- so anything that depends on it is labelled STRUCTURAL, never LOGICAL.

    A gap is dropped only when it matches that construction exactly: no market covers it, both
    endpoints *are* covered (one bracket stops, the next resumes), and it is no wider than one
    grid step. Returns ``None`` when nothing qualifies.
    """
    points = [
        atom.low for atom in space.atoms if atom.kind is AtomKind.POINT and atom.low is not None
    ]
    grid = settlement_grid(points)
    coverage = space.coverage()
    # GAP atoms always sit between two POINT atoms, so index +/- 1 are its endpoints.
    dropped = {
        index
        for index, atom in enumerate(space.atoms)
        if atom.kind is AtomKind.GAP
        and coverage[index] == 0
        and coverage[index - 1] > 0
        and coverage[index + 1] > 0
        and (width := atom.width) is not None
        and width <= grid
    }
    if not dropped:
        return None
    kept = [index for index in range(space.size) if index not in dropped]
    renumber = {old: new for new, old in enumerate(kept)}
    return OutcomeSpace(
        atoms=tuple(space.atoms[index] for index in kept),
        yes_atoms={
            ticker: frozenset(renumber[index] for index in atoms)
            for ticker, atoms in space.yes_atoms.items()
        },
        epsilon=grid,
    )


def categorical_space(tickers: Sequence[str], *, exhaustive: bool) -> OutcomeSpace:
    """One atom per outcome, plus the residual atom unless the list is known to be complete."""
    atoms = [Atom(AtomKind.OUTCOME, ticker) for ticker in tickers]
    if not exhaustive:
        atoms.append(Atom(AtomKind.RESIDUAL, "none of the listed outcomes"))
    yes_atoms = {ticker: frozenset({index}) for index, ticker in enumerate(tickers)}
    return OutcomeSpace(tuple(atoms), yes_atoms)
