from __future__ import annotations

from datetime import timedelta

import pytest

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.arb.opportunity import Opportunity, OpportunityTracker, opportunity_id
from karb.core.fixed import Cash
from karb.market.book import Side
from karb.structure.classify import Tier, classify_event
from tests.support import NOW, SERIES, book, event, market


def overround(collateral: str = "", observed_at=NOW) -> Opportunity:  # type: ignore[no-untyped-def]
    ev = event(
        [market("A"), market("B"), market("C")], mutually_exclusive=True, collateral=collateral
    )
    books = {t: book(t, yes=[("0.40", "50")], no=[("0.55", "50")]) for t in ev.tickers}
    structure = classify_event(ev, SERIES).structure
    assert structure is not None
    (opp,) = detect(
        EventSnapshot(structure, books, frozenset(ev.tickers), observed_at), DetectConfig()
    ).opportunities
    return opp


def test_id_ignores_leg_order_but_not_tier() -> None:
    legs = [("A", Side.NO), ("B", Side.NO)]
    assert opportunity_id("EV", Tier.LOGICAL, legs) == opportunity_id(
        "EV", Tier.LOGICAL, legs[::-1]
    )
    assert opportunity_id("EV", Tier.LOGICAL, legs) != opportunity_id("EV", Tier.STRUCTURAL, legs)


def test_tracker_confirms_on_consecutive_sightings_and_ends_when_gone() -> None:
    tracker = OpportunityTracker(confirmations=2)
    opp = overround()
    tracker.observe("EV", [opp], NOW)
    (sighting,) = tracker.live()
    assert not tracker.is_confirmed(sighting)

    tracker.observe("OTHER", [], NOW + timedelta(seconds=1))  # other events leave it alone
    tracker.observe("EV", [opp], NOW + timedelta(seconds=5))
    (sighting,) = tracker.live()
    assert tracker.is_confirmed(sighting)
    assert sighting.age_seconds == 5

    tracker.observe("EV", [], NOW + timedelta(seconds=10))
    assert tracker.live() == []
    assert len(tracker.ended) == 1


def test_capital_views() -> None:
    plain = overround()
    assert plain.netted_capital is None
    assert plain.edge == pytest.approx(7.48 / 92.52)
    assert plain.apr == pytest.approx((7.48 / 92.52) / (30 / 365.25))

    netted = overround(collateral="MECNET")
    # The guaranteed $100 payout exceeds the $92.52 cost: collateral return would lock nothing.
    assert netted.netted_capital == Cash.ZERO
