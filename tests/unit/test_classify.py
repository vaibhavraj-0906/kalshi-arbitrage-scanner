from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from itertools import pairwise

import pytest

from karb.market.model import MarketInfo, SeriesInfo
from karb.structure.classify import (
    Exclusion,
    StructureKind,
    Tier,
    classify_event,
    tradeable_tickers,
)
from karb.structure.intervals import AtomKind
from tests.support import NOW, SERIES, event, fixture_event, fixture_series, market


def test_kxinx_range_ladder_tiles_on_its_strike_grid() -> None:
    ev = fixture_event("events_KXINX.json")
    result = classify_event(ev, fixture_series()["KXINX"])
    structure = result.structure
    assert result.exclusion is None and structure is not None
    assert structure.kind is StructureKind.INTERVAL and ev.mutually_exclusive
    assert list(structure.spaces) == [Tier.LOGICAL, Tier.STRUCTURAL]

    logical = structure.spaces[Tier.LOGICAL]
    assert not logical.overlaps()
    holes = [logical.atoms[i] for i in logical.holes()]
    assert holes, "brackets written as [7225, 7249.9999] leave sub-tick gaps"
    assert all(atom.kind is AtomKind.GAP and atom.width == D("0.0001") for atom in holes)

    structural = structure.spaces[Tier.STRUCTURAL]
    assert structural.epsilon == D("0.0001")
    assert structural.is_partition
    assert all(logical.run(t) is not None for t in ev.tickers)


def test_kxbtcd_threshold_ladder_is_strictly_nested() -> None:
    ev = fixture_event("events_KXBTCD.json")
    structure = classify_event(ev, fixture_series()["KXBTCD"]).structure
    assert structure is not None
    assert structure.kind is StructureKind.INTERVAL and not ev.mutually_exclusive
    assert list(structure.spaces) == [Tier.LOGICAL]
    assert {m.strike_type for m in ev.markets} == {"greater"}
    space = structure.spaces[Tier.LOGICAL]
    by_strike = sorted(ev.markets, key=lambda m: m.floor_strike or D(0))
    for lower, higher in pairwise(by_strike):
        assert space.yes_atoms[higher.ticker] < space.yes_atoms[lower.ticker]


def test_custom_mutually_exclusive_event_keeps_a_residual_outcome() -> None:
    ev = fixture_event("event_KXNEXTDNCCHAIR-45.json")
    series = fixture_series()["KXNEXTDNCCHAIR"]
    structure = classify_event(ev, series).structure
    assert structure is not None and structure.kind is StructureKind.CATEGORICAL
    assert list(structure.spaces) == [Tier.LOGICAL]
    assert structure.spaces[Tier.LOGICAL].size == len(ev.markets) + 1

    asserted = classify_event(
        ev, series, asserted_exhaustive=frozenset({"KXNEXTDNCCHAIR"})
    ).structure
    assert asserted is not None
    assert list(asserted.spaces) == [Tier.LOGICAL, Tier.ASSERTED]
    assert asserted.spaces[Tier.ASSERTED].is_partition


def test_single_market_event_is_excluded() -> None:
    result = classify_event(
        fixture_event("event_KXELONMARS-99.json"), fixture_series()["KXELONMARS"]
    )
    assert result.exclusion is Exclusion.SINGLE_MARKET


@pytest.mark.parametrize(
    ("markets", "mutually_exclusive", "exclusion"),
    [
        ([market("A", is_mve=True), market("B")], True, Exclusion.MULTIVARIATE),
        ([market("A", market_type="scalar"), market("B")], True, Exclusion.NON_BINARY),
        ([market("A", result="yes"), market("B")], True, Exclusion.RESOLVED),
        (
            [market("A", strike_type="custom"), market("B", strike_type="custom")],
            False,
            Exclusion.NO_STRUCTURE,
        ),
        (
            [market("A", strike_type="functional"), market("B", strike_type="functional")],
            False,
            Exclusion.UNSUPPORTED_STRIKES,
        ),
        (
            [market("A", strike_type="greater"), market("B", strike_type="greater", floor="1")],
            False,
            Exclusion.INVALID_STRIKES,
        ),
        (
            [
                market("A", strike_type="greater", floor="1"),
                market("B", strike_type="greater", floor="2"),
            ],
            True,
            Exclusion.CONTRADICTORY,
        ),
        (
            [
                market("J40", strike_type="greater", floor="39.5", participant={"player": "coker"}),
                market("M50", strike_type="greater", floor="49.5", participant={"player": "moore"}),
            ],
            False,
            Exclusion.MIXED_PARTICIPANTS,
        ),
        (
            [
                market(
                    "Y27", strike_type="greater_or_equal", floor="1e8", closes_in=timedelta(100)
                ),
                market(
                    "Y28", strike_type="greater_or_equal", floor="1e8", closes_in=timedelta(465)
                ),
            ],
            False,
            Exclusion.STAGGERED_SETTLEMENT,
        ),
        (
            # "Jacksonville wins by over 3.5" and "Cleveland wins by over 3.5", unattributed.
            [
                market("JAC", strike_type="greater", floor="3.5"),
                market("CLE", strike_type="greater", floor="3.50"),
            ],
            False,
            Exclusion.DUPLICATE_STRIKES,
        ),
        (
            # KXHOUSEPOPVOTEMARGIN-27NOV03: brackets [-1, 0], [0, 2], [2, 4] on one margin.
            [
                market("R", strike_type="between", floor="-1", cap="0"),
                market("D1", strike_type="between", floor="0", cap="2"),
                market("D2", strike_type="between", floor="2", cap="4"),
            ],
            True,
            Exclusion.AMBIGUOUS_BOUNDARIES,
        ),
        (
            [
                market("D1", strike_type="between", floor="0", cap="2"),
                market("D2", strike_type="between", floor="2", cap="4"),
            ],
            False,
            Exclusion.AMBIGUOUS_BOUNDARIES,
        ),
    ],
)
def test_exclusions(
    markets: list[MarketInfo], mutually_exclusive: bool, exclusion: Exclusion
) -> None:
    result = classify_event(event(markets, mutually_exclusive=mutually_exclusive), SERIES)
    assert result.structure is None
    assert result.exclusion is exclusion


def test_unmodelled_fees_exclude_an_otherwise_eligible_event() -> None:
    ev = event([market("A"), market("B")], mutually_exclusive=True)
    result = classify_event(ev, SeriesInfo("SER", "", "", "flat", D(1)))
    assert result.exclusion is Exclusion.FEES
    assert "not modelled" in result.detail


def test_tradeable_tickers() -> None:
    ev = event(
        [
            market("OK"),
            market("CLOSED", status="closed"),
            market("SOON", closes_in=timedelta(minutes=2)),
            market("SHARD3", exchange_index=3),
        ]
    )
    assert tradeable_tickers(ev, now=NOW, trading_shards=frozenset({0})) == {"OK"}
    assert tradeable_tickers(ev, now=NOW) == {"OK", "SHARD3"}
