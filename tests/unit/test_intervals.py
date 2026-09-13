from __future__ import annotations

from decimal import Decimal as D

import pytest

from karb.market.book import Side
from karb.structure.intervals import (
    AtomKind,
    StructureError,
    categorical_space,
    interval_space,
    settlement_grid,
    structural_space,
    yes_interval,
)


def iv(strike_type: str, floor: str | None = None, cap: str | None = None):  # type: ignore[no-untyped-def]
    return yes_interval(
        strike_type, None if floor is None else D(floor), None if cap is None else D(cap)
    )


@pytest.mark.parametrize(
    ("strike_type", "floor", "cap", "inside", "outside"),
    [
        ("greater", "10", None, ["10.0001", "1e6"], ["10", "9"]),
        ("greater_or_equal", "10", None, ["10", "11"], ["9.9999"]),
        ("less", None, "10", ["9.9999", "-5"], ["10", "11"]),
        ("less_or_equal", None, "10", ["10"], ["10.0001"]),
        ("between", "10", "19.9999", ["10", "15", "19.9999"], ["9.9999", "20"]),
    ],
)
def test_strike_semantics(
    strike_type: str, floor: str | None, cap: str | None, inside: list[str], outside: list[str]
) -> None:
    interval = iv(strike_type, floor, cap)
    assert all(interval.contains(D(v)) for v in inside)
    assert not any(interval.contains(D(v)) for v in outside)


@pytest.mark.parametrize(
    ("strike_type", "floor", "cap"),
    [
        ("greater", None, None),
        ("less", "5", None),
        ("between", "20", "10"),
        ("custom", "1", "2"),
        ("functional", None, None),
        ("nonsense", "1", None),
    ],
)
def test_rejects_strikes_without_interval_meaning(
    strike_type: str, floor: str | None, cap: str | None
) -> None:
    with pytest.raises(StructureError):
        iv(strike_type, floor, cap)
    with pytest.raises(StructureError):
        yes_interval(None, D(1), D(2))


def test_atoms_of_a_simple_partition() -> None:
    space = interval_space(
        {"LOW": iv("less", cap="10"), "MID": iv("between", "10", "20"), "HIGH": iv("greater", "20")}
    )
    assert [atom.kind for atom in space.atoms] == [
        AtomKind.LOWER_TAIL,
        AtomKind.POINT,
        AtomKind.GAP,
        AtomKind.POINT,
        AtomKind.UPPER_TAIL,
    ]
    assert space.yes_atoms == {"LOW": {0}, "MID": {1, 2, 3}, "HIGH": {4}}
    assert space.is_partition
    assert space.pays("LOW", Side.NO, 3) and not space.pays("LOW", Side.YES, 3)
    assert space.run("MID") == (1, 3)
    assert structural_space(space) is None


def test_structural_space_drops_exactly_the_tiling_gap() -> None:
    logical = interval_space(
        {
            "LOW": iv("less", cap="10"),
            "B1": iv("between", "10", "19.99"),
            "B2": iv("between", "20", "29.99"),
            "HIGH": iv("greater", "29.99"),
        }
    )
    assert [logical.atoms[i].label for i in logical.holes()] == ["(19.99, 20)"]
    structural = structural_space(logical)
    assert structural is not None
    assert structural.epsilon == D("0.01")
    assert structural.is_partition
    assert structural.size == logical.size - 1
    assert all(structural.run(t) is not None for t in structural.tickers)


def test_wide_holes_are_real_outcomes_and_stay() -> None:
    space = interval_space({"LOW": iv("less", cap="10"), "HIGH": iv("greater", "20")})
    assert structural_space(space) is None


def test_a_gap_beside_an_uncovered_point_stays() -> None:
    # X = 10 resolves every market NO, so the gap (10, 10.01) is not a tiling artefact.
    space = interval_space(
        {
            "LOW": iv("less", cap="10"),
            "MID": iv("between", "10.01", "20"),
            "HIGH": iv("greater", "20"),
        }
    )
    assert structural_space(space) is None


def test_settlement_grid() -> None:
    assert settlement_grid([D("7225"), D("7249.9999"), D("7250")]) == D("0.0001")
    assert settlement_grid([D("7250"), D("100")]) == D("1")
    assert settlement_grid([D("67599.99")]) == D("0.01")


def test_categorical_space_residual() -> None:
    open_ended = categorical_space(["A", "B"], exhaustive=False)
    assert open_ended.size == 3
    assert open_ended.atoms[-1].kind is AtomKind.RESIDUAL
    assert open_ended.holes() == (2,)
    assert categorical_space(["A", "B"], exhaustive=True).is_partition
