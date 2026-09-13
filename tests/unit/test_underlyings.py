"""Regressions from live data: strike markets that do not share an underlying.

A full-universe confirmation run on 2026-09-13 "verified" 637 opportunities -- among them a
guaranteed $34,122 on NFL receiving-yards props. Every large one came from treating markets
about different players, teams or deadlines as strikes on one number. Each shape is pinned here.
"""

from __future__ import annotations

from datetime import timedelta

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.arb.opportunity import Opportunity
from karb.structure.classify import Exclusion, classify_event, split_by_participant
from tests.support import NOW, SERIES, book, event, market

COKER = {"football_player": "85b609a0-24c7-42cf"}
MOORE = {"football_player": "3f1c2d9e-0000-4a1b"}
JAC = {"football_team": "61a75ab7-eaf0-4601"}
CLE = {"football_team": "0c3b7e21-99aa-4c0d"}


def test_player_props_split_into_per_participant_ladders() -> None:
    ev = event(
        [
            market("COKER-15", strike_type="greater", floor="14.5", participant=COKER),
            market("COKER-40", strike_type="greater", floor="39.5", participant=COKER),
            market("MOORE-15", strike_type="greater", floor="14.5", participant=MOORE),
            market("MOORE-40", strike_type="greater", floor="39.5", participant=MOORE),
        ]
    )
    assert classify_event(ev, SERIES).exclusion is Exclusion.MIXED_PARTICIPANTS

    parts = split_by_participant(ev)
    assert [part.event_ticker for part in parts] == [
        "EV#football_player:85b609a0",
        "EV#football_player:3f1c2d9e",
    ]
    for part in parts:
        assert len(part.markets) == 2
        assert classify_event(part, SERIES).structure is not None


def test_opposite_teams_spreads_are_not_one_ladder() -> None:
    """Jacksonville -3.5 bid far above Cleveland -3.5's ask is not an arbitrage.

    Read as one ladder, the two "wins by over 3.5" markets have identical YES-sets, so buying
    Cleveland's YES and Jacksonville's NO looks like a riskless 43 cents. It is a bet on the game.
    """
    ev = event(
        [
            market("JAC-4", strike_type="greater", floor="3.5", participant=JAC),
            market("JAC-7", strike_type="greater", floor="6.5", participant=JAC),
            market("CLE-4", strike_type="greater", floor="3.5", participant=CLE),
            market("CLE-7", strike_type="greater", floor="6.5", participant=CLE),
        ]
    )
    books = {
        "JAC-4": book("JAC-4", yes=[("0.70", "50")], no=[("0.28", "50")]),
        "JAC-7": book("JAC-7", yes=[("0.55", "50")], no=[("0.43", "50")]),
        "CLE-4": book("CLE-4", yes=[("0.25", "50")], no=[("0.73", "50")]),
        "CLE-7": book("CLE-7", yes=[("0.15", "50")], no=[("0.83", "50")]),
    }
    assert classify_event(ev, SERIES).exclusion is Exclusion.MIXED_PARTICIPANTS

    found: list[Opportunity] = []
    for part in split_by_participant(ev):
        structure = classify_event(part, SERIES).structure
        assert structure is not None
        snapshot = EventSnapshot(
            structure, {t: books[t] for t in part.tickers}, frozenset(part.tickers), NOW
        )
        found.extend(detect(snapshot, DetectConfig()).opportunities)
    assert found == []


def test_deadline_questions_reusing_one_strike_are_excluded() -> None:
    # KXYTUBESUBSISHOWSPEED: ">= 100,000,000 subscribers" before 2027, 2028, 2029, 2030.
    ev = event(
        [
            market(
                "Y27", strike_type="greater_or_equal", floor="100000000", closes_in=timedelta(100)
            ),
            market(
                "Y28", strike_type="greater_or_equal", floor="100000000", closes_in=timedelta(465)
            ),
        ]
    )
    assert classify_event(ev, SERIES).exclusion is Exclusion.STAGGERED_SETTLEMENT


def test_multi_year_targets_are_excluded() -> None:
    # USCLIMATE: emissions at most 4909.9 by 2025, at most 3317.5 by 2030 -- two quantities.
    ev = event(
        [
            market("BY2025", strike_type="less_or_equal", cap="4909.9", closes_in=timedelta(100)),
            market("BY2030", strike_type="less_or_equal", cap="3317.5", closes_in=timedelta(1500)),
        ]
    )
    assert classify_event(ev, SERIES).exclusion is Exclusion.STAGGERED_SETTLEMENT


def test_categorical_events_are_never_split() -> None:
    ev = event(
        [market("A", participant={"team": "a"}), market("B", participant={"team": "b"})],
        mutually_exclusive=True,
    )
    assert split_by_participant(ev) == (ev,)
    assert classify_event(ev, SERIES).structure is not None


def test_single_underlying_ladders_are_untouched() -> None:
    ev = event(
        [
            market("T3.25", strike_type="greater", floor="3.25"),
            market("T3.50", strike_type="greater", floor="3.50"),
        ]
    )
    assert split_by_participant(ev) == (ev,)
    assert classify_event(ev, SERIES).structure is not None
