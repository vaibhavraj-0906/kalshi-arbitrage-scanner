from __future__ import annotations

from karb.arb.screen import _max_exclusive_weight, _min_cover_cost, screen_event
from karb.core.fixed import Price
from karb.market.model import EventInfo
from karb.structure.classify import Tier, classify_event
from tests.support import SERIES, event, market


def run_screen(ev: EventInfo, tradeable: frozenset[str] | None = None):  # type: ignore[no-untyped-def]
    structure = classify_event(ev, SERIES).structure
    assert structure is not None
    quotes = {m.ticker: m.quote for m in ev.markets}
    return screen_event(
        structure, quotes, frozenset(ev.tickers) if tradeable is None else tradeable
    )


def test_max_exclusive_weight() -> None:
    assert _max_exclusive_weight([]) == 0
    assert _max_exclusive_weight([(0, 1, 5), (2, 3, 6), (1, 2, 10)]) == 11
    assert _max_exclusive_weight([(0, 5, 3), (1, 1, 2), (2, 2, 2)]) == 4


def test_min_cover_cost() -> None:
    assert _min_cover_cost(4, [(0, 1, 3), (2, 3, 3), (0, 3, 7)]) == 6
    assert _min_cover_cost(4, [(0, 2, 4), (3, 3, 2), (1, 3, 1)]) == 5
    assert _min_cover_cost(1, [(0, 0, 9)]) == 9
    assert _min_cover_cost(3, [(0, 0, 1), (2, 2, 1)]) is None


def test_categorical_bids_over_a_dollar() -> None:
    ev = event(
        [
            market("A", bid="0.40", ask="0.45"),
            market("B", bid="0.40", ask="0.45"),
            market("C", bid="0.30", ask="0.35"),
        ],
        mutually_exclusive=True,
    )
    hits = run_screen(ev)
    assert [(h.tier, h.rule, h.gross_edge) for h in hits] == [
        (Tier.LOGICAL, "bids over $1", Price.parse("0.10"))
    ]


def test_untradeable_markets_do_not_count() -> None:
    ev = event(
        [
            market("A", bid="0.40", ask="0.45"),
            market("B", bid="0.40", ask="0.45"),
            market("C", bid="0.30", ask="0.35"),
        ],
        mutually_exclusive=True,
    )
    assert run_screen(ev, frozenset({"A", "B"})) == []


def test_ladder_bid_above_containing_ask() -> None:
    ev = event(
        [
            market("K10", strike_type="greater", floor="10", bid="0.45", ask="0.50"),
            market("K20", strike_type="greater", floor="20", bid="0.60", ask="0.65"),
        ]
    )
    hits = run_screen(ev)
    assert [(h.rule, h.gross_edge) for h in hits] == [
        ("bid above containing ask", Price.parse("0.10"))
    ]


def test_partition_asks_under_a_dollar_only_under_structural() -> None:
    ev = event(
        [
            market("LOW", strike_type="less", cap="10", bid="0.15", ask="0.20"),
            market("B1", strike_type="between", floor="10", cap="19.99", bid="0.15", ask="0.20"),
            market("B2", strike_type="between", floor="20", cap="29.99", bid="0.15", ask="0.20"),
            market("HIGH", strike_type="greater", floor="29.99", bid="0.15", ask="0.20"),
        ],
        mutually_exclusive=True,
    )
    hits = run_screen(ev)
    assert [(h.tier, h.rule, h.gross_edge) for h in hits] == [
        (Tier.STRUCTURAL, "covering asks under $1", Price.parse("0.20"))
    ]
