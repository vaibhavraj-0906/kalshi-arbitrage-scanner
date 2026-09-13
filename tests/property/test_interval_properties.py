from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from karb.structure.intervals import (
    Atom,
    AtomKind,
    Interval,
    OutcomeSpace,
    interval_space,
    structural_space,
    yes_interval,
)

INTERVAL_TYPES = ["greater", "greater_or_equal", "less", "less_or_equal", "between"]


@st.composite
def interval_sets(draw: st.DrawFn) -> tuple[dict[str, Interval], list[Decimal], Decimal]:
    grid = Decimal(1).scaleb(-draw(st.integers(min_value=0, max_value=3)))
    raw = sorted(draw(st.sets(st.integers(min_value=-50, max_value=50), min_size=1, max_size=6)))
    strikes = [grid * value for value in raw]
    intervals: dict[str, Interval] = {}
    for index in range(draw(st.integers(min_value=1, max_value=6))):
        low = draw(st.sampled_from(strikes))
        high = draw(st.sampled_from([s for s in strikes if s >= low]))
        intervals[f"M{index}"] = yes_interval(draw(st.sampled_from(INTERVAL_TYPES)), low, high)
    return intervals, strikes, grid


def _atom_contains(atom: Atom, x: Decimal) -> bool:
    if atom.kind is AtomKind.POINT:
        return x == atom.low
    if atom.kind is AtomKind.GAP:
        assert atom.low is not None and atom.high is not None
        return atom.low < x < atom.high
    if atom.kind is AtomKind.LOWER_TAIL:
        assert atom.high is not None
        return x < atom.high
    assert atom.kind is AtomKind.UPPER_TAIL and atom.low is not None
    return x > atom.low


def _probes(strikes: list[Decimal], step: Decimal) -> set[Decimal]:
    values = {strikes[0] - 100, strikes[-1] + 100}
    for s in strikes:
        values |= {s, s - step, s + step, s - step / 2, s + step / 2}
    return values


def _atom_of(space: OutcomeSpace, x: Decimal) -> int:
    matches = [i for i, atom in enumerate(space.atoms) if _atom_contains(atom, x)]
    assert len(matches) == 1, (x, matches)
    return matches[0]


@given(interval_sets())
def test_atoms_partition_the_line_and_agree_with_every_interval(
    case: tuple[dict[str, Interval], list[Decimal], Decimal],
) -> None:
    intervals, strikes, grid = case
    space = interval_space(intervals)
    for x in _probes(strikes, grid):
        atom = _atom_of(space, x)
        for ticker, interval in intervals.items():
            assert (atom in space.yes_atoms[ticker]) == interval.contains(x)


@given(interval_sets())
def test_structural_space_only_drops_gaps_that_hold_no_grid_value(
    case: tuple[dict[str, Interval], list[Decimal], Decimal],
) -> None:
    intervals, _strikes, _grid = case
    logical = interval_space(intervals)
    structural = structural_space(logical)
    if structural is None:
        return
    assert structural.epsilon is not None
    kept = {atom.label for atom in structural.atoms}
    for atom in logical.atoms:
        if atom.label in kept:
            continue
        assert atom.kind is AtomKind.GAP
        assert atom.width == structural.epsilon
        assert not any(intervals[t].contains((atom.low + atom.high) / 2) for t in intervals)  # type: ignore[operator]


@given(
    st.integers(min_value=0, max_value=4),
    st.integers(min_value=1, max_value=10),
    st.integers(min_value=2, max_value=40),
    st.integers(min_value=-1000, max_value=1000),
)
def test_kalshi_style_tiled_ladders_partition_under_structural(
    decimals: int, brackets: int, width: int, start: int
) -> None:
    eps = Decimal(1).scaleb(-decimals)
    edges = [eps * (start + i * width) for i in range(brackets + 1)]
    ladder = {"LOW": yes_interval("less", None, edges[0])}
    for i in range(brackets):
        ladder[f"B{i}"] = yes_interval("between", edges[i], edges[i + 1] - eps)
    ladder["HIGH"] = yes_interval("greater", edges[-1] - eps, None)

    logical = interval_space(ladder)
    assert not logical.overlaps()
    assert (structural_space(logical) or logical).is_partition
