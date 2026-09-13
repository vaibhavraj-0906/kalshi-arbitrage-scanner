from __future__ import annotations

from bisect import bisect_left

import pytest
from hypothesis import given
from hypothesis import strategies as st

from karb.core.fixed import PRICE_SCALE, Price, Rounding
from karb.market.grid import GridError, PriceGrid
from tests.support import fixture_event, load_fixture

# Every structure in Kalshi's fixed-point documentation (getting_started/fixed_point_migration).
DOCUMENTED_STRUCTURES: dict[str, list[tuple[str, str, str]]] = {
    "linear_cent": [("0.0000", "1.0000", "0.0100")],
    "deci_cent": [("0.0000", "1.0000", "0.0010")],
    "tapered_deci_cent": [
        ("0.0000", "0.1000", "0.0010"),
        ("0.1000", "0.9000", "0.0100"),
        ("0.9000", "1.0000", "0.0010"),
    ],
    "center_whole_edge_half_cent": [
        ("0.0000", "0.1000", "0.0050"),
        ("0.1000", "0.9000", "0.0100"),
        ("0.9000", "1.0000", "0.0050"),
    ],
    "center_whole_edge_quint_cent": [
        ("0.0000", "0.1000", "0.0020"),
        ("0.1000", "0.9000", "0.0100"),
        ("0.9000", "1.0000", "0.0020"),
    ],
    "center_half_edge_half_cent": [("0.0000", "1.0000", "0.0050")],
    "center_half_edge_quint_cent": [
        ("0.0000", "0.1000", "0.0020"),
        ("0.1000", "0.9000", "0.0050"),
        ("0.9000", "1.0000", "0.0020"),
    ],
    "center_half_edge_deci_cent": [
        ("0.0000", "0.1000", "0.0010"),
        ("0.1000", "0.9000", "0.0050"),
        ("0.9000", "1.0000", "0.0010"),
    ],
    "center_quint_edge_quint_cent": [("0.0000", "1.0000", "0.0020")],
    "center_quint_edge_deci_cent": [
        ("0.0000", "0.1000", "0.0010"),
        ("0.1000", "0.9000", "0.0020"),
        ("0.9000", "1.0000", "0.0010"),
    ],
    "center_centi_edge_centi_cent": [("0.0000", "1.0000", "0.0001")],
    "center_deci_edge_centi_cent": [
        ("0.0000", "0.0100", "0.0001"),
        ("0.0100", "0.9900", "0.0010"),
        ("0.9900", "1.0000", "0.0001"),
    ],
}

GRIDS = {name: PriceGrid.from_strings(ranges) for name, ranges in DOCUMENTED_STRUCTURES.items()}


@pytest.mark.parametrize("name", sorted(DOCUMENTED_STRUCTURES))
def test_whole_cents_are_valid_in_every_structure(name: str) -> None:
    # Documented guarantee: "Whole-cent prices are valid in every structure."
    grid = GRIDS[name]
    assert all(grid.contains(Price(cents * 100)) for cents in range(101))


@pytest.mark.parametrize(
    ("name", "count"),
    [("linear_cent", 101), ("deci_cent", 1001), ("center_deci_edge_centi_cent", 1181)],
)
def test_valid_price_counts(name: str, count: int) -> None:
    prices = list(GRIDS[name].prices())
    assert len(prices) == count
    assert prices == sorted(set(prices))


def test_snap_in_a_tapered_grid() -> None:
    grid = GRIDS["tapered_deci_cent"]
    assert grid.snap(Price.parse("0.1234"), Rounding.FLOOR) == Price.parse("0.1200")
    assert grid.snap(Price.parse("0.1234"), Rounding.CEIL) == Price.parse("0.1300")
    assert grid.snap(Price.parse("0.0456"), Rounding.FLOOR) == Price.parse("0.0450")
    assert grid.snap(Price.parse("0.0456"), Rounding.CEIL) == Price.parse("0.0460")
    assert not grid.contains(Price.parse("0.1234"))


def test_rejects_malformed_bands() -> None:
    with pytest.raises(GridError, match="contiguous"):
        PriceGrid.from_strings([("0.0000", "0.5000", "0.0100"), ("0.6000", "1.0000", "0.0100")])
    with pytest.raises(GridError, match="whole number of steps"):
        PriceGrid.from_strings([("0.0000", "1.0000", "0.0300")])
    with pytest.raises(GridError):
        PriceGrid.from_strings([])


def test_live_markets_carry_valid_grids() -> None:
    for name in ("events_KXINX.json", "events_KXBTCD.json", "event_KXNEXTDNCCHAIR-45.json"):
        for market in fixture_event(name).markets:
            assert market.grid is not None, market.ticker
            assert not market.integrity_issues, market.integrity_issues
    for raw in load_fixture("markets_page.json")["markets"]:
        ranges = [(r["start"], r["end"], r["step"]) for r in raw["price_ranges"]]
        assert PriceGrid.from_strings(ranges).contains(Price.parse("0.5000"))


@given(st.sampled_from(sorted(GRIDS)), st.integers(min_value=0, max_value=PRICE_SCALE))
def test_snap_brackets_price_with_nothing_valid_in_between(name: str, raw: int) -> None:
    grid = GRIDS[name]
    price = Price(raw)
    floor = grid.snap(price, Rounding.FLOOR)
    ceil = grid.snap(price, Rounding.CEIL)
    assert floor is not None and ceil is not None
    assert grid.contains(floor) and grid.contains(ceil)
    assert floor <= price <= ceil
    valid = [p.raw for p in grid.prices()]
    if grid.contains(price):
        assert floor == ceil == price
    else:
        # floor and ceil are adjacent entries of the valid-price list
        index = bisect_left(valid, raw)
        assert valid[index - 1] == floor.raw and valid[index] == ceil.raw
