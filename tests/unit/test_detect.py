"""Planted arbitrage, priced by hand.

Every expected number below is derived in the comment beside it from Kalshi's fee formula
(0.07 x C x P x (1 - P), rounded up to $0.000001) and cent balance alignment.
"""

from __future__ import annotations

from karb.arb.detect import DetectConfig, EventSnapshot, detect, whole_contract_candidates
from karb.arb.lp import LpSolution
from karb.arb.opportunity import ArbKind
from karb.core.fixed import Cash, Qty
from karb.market.book import OrderBook, Side
from karb.market.model import EventInfo
from karb.structure.classify import Tier, classify_event
from tests.support import NOW, SERIES, book, event, market


def snapshot(
    ev: EventInfo,
    books: dict[str, OrderBook],
    *,
    asserted: frozenset[str] = frozenset(),
    tradeable: frozenset[str] | None = None,
) -> EventSnapshot:
    structure = classify_event(ev, SERIES, asserted_exhaustive=asserted).structure
    assert structure is not None
    return EventSnapshot(
        structure, books, frozenset(ev.tickers) if tradeable is None else tradeable, NOW
    )


def three_way(**kwargs: bool) -> EventInfo:
    return event([market("A"), market("B"), market("C")], mutually_exclusive=True, **kwargs)  # type: ignore[arg-type]


def test_overround_on_a_mutually_exclusive_event() -> None:
    ev = three_way()
    # YES bids of 0.40 on three exclusive outcomes sum to 1.20: NO asks sit at 0.60.
    books = {t: book(t, yes=[("0.40", "50")], no=[("0.55", "50")]) for t in ev.tickers}
    detection = detect(snapshot(ev, books), DetectConfig())
    (opp,) = detection.opportunities
    assert opp.kind is ArbKind.OVERROUND and opp.tier is Tier.LOGICAL
    assert {(leg.ticker, leg.side, leg.order.qty) for leg in opp.basket.legs} == {
        (t, Side.NO, Qty.contracts(50)) for t in "ABC"
    }
    # Per leg: notional 50 x 0.60 = $30, fee ceil(0.07 x 50 x 0.6 x 0.4) = $0.84, on the cent grid.
    assert opp.cost == Cash(92_520_000)
    assert opp.basket.min_payoff == Cash(100_000_000)  # someone listed wins: two NOs pay $50 each
    assert opp.basket.max_payoff == Cash(150_000_000)  # nobody listed wins: all three pay
    assert opp.guaranteed_pnl == Cash(7_480_000)


def test_monotone_violation_on_a_threshold_ladder() -> None:
    ev = event(
        [
            market("K10", strike_type="greater", floor="10"),
            market("K20", strike_type="greater", floor="20"),
        ]
    )
    books = {
        "K10": book("K10", yes=[("0.45", "30")], no=[("0.50", "30")]),  # P(X > 10) offered at 0.50
        "K20": book("K20", yes=[("0.60", "30")], no=[("0.35", "30")]),  # P(X > 20) bid at 0.60
    }
    (opp,) = detect(snapshot(ev, books), DetectConfig()).opportunities
    assert opp.kind is ArbKind.MONOTONE and opp.tier is Tier.LOGICAL
    assert {(leg.ticker, leg.side) for leg in opp.basket.legs} == {
        ("K10", Side.YES),
        ("K20", Side.NO),
    }
    # YES K10: $15 + fee $0.525 = $15.525 -> aligned to $15.53.
    # NO  K20: $12 + fee $0.504 = $12.504 -> aligned to $12.51.
    assert opp.cost == Cash(28_040_000)
    assert opp.basket.min_payoff == Cash(30_000_000)
    assert opp.guaranteed_pnl == Cash(1_960_000)


def test_underround_on_a_tiled_ladder_needs_the_structural_tier() -> None:
    ev = event(
        [
            market("LOW", strike_type="less", cap="10"),
            market("B1", strike_type="between", floor="10", cap="19.99"),
            market("B2", strike_type="between", floor="20", cap="29.99"),
            market("HIGH", strike_type="greater", floor="29.99"),
        ],
        mutually_exclusive=True,
    )
    books = {
        t: book(t, yes=[("0.15", "25")], no=[("0.80", "25")]) for t in ev.tickers
    }  # YES asks 0.20
    detection = detect(snapshot(ev, books), DetectConfig())
    assert not detection.lp[Tier.LOGICAL].found  # a settlement in (19.99, 20) would pay nothing
    (opp,) = detection.opportunities
    assert opp.kind is ArbKind.UNDERROUND and opp.tier is Tier.STRUCTURAL
    # Per leg: $5 + fee ceil(0.07 x 25 x 0.2 x 0.8) = $0.28 -> $5.28, on the cent grid.
    assert opp.cost == Cash(21_120_000)
    assert opp.guaranteed_pnl == Cash(3_880_000)


def test_residual_outcome_blocks_underround_until_exhaustiveness_is_asserted() -> None:
    ev = three_way()
    books = {
        t: book(t, yes=[("0.15", "20")], no=[("0.80", "20")]) for t in ev.tickers
    }  # asks sum 0.60
    assert detect(snapshot(ev, books), DetectConfig()).opportunities == ()
    (opp,) = detect(snapshot(ev, books, asserted=frozenset({"EV"})), DetectConfig()).opportunities
    assert opp.tier is Tier.ASSERTED and opp.kind is ArbKind.UNDERROUND


def test_crossed_and_missing_books_are_excluded_and_reported() -> None:
    ev = three_way()
    books = {t: book(t, yes=[("0.40", "50")], no=[("0.55", "50")]) for t in "AB"}
    books["C"] = book("C", yes=[("0.50", "10")], no=[("0.50", "10")])
    detection = detect(snapshot(ev, books), DetectConfig())
    assert detection.integrity == ("C: crossed book",)
    assert detection.opportunities == ()  # A and B alone: bids sum to 0.80

    del books["C"]
    assert detect(snapshot(ev, books), DetectConfig()).integrity == (
        "C: no order book in snapshot",
    )


def test_minimum_profit_threshold() -> None:
    ev = three_way()
    books = {t: book(t, yes=[("0.40", "50")], no=[("0.55", "50")]) for t in ev.tickers}
    assert detect(snapshot(ev, books), DetectConfig(min_profit=Cash(8_000_000))).opportunities == ()


def test_depth_is_taken_only_while_it_pays() -> None:
    ev = three_way()
    # Second level: NO asks at 0.70 x 3 = 2.10 for a guaranteed 2.00 -- never worth taking.
    books = {
        t: book(t, yes=[("0.40", "10"), ("0.30", "100")], no=[("0.55", "50")]) for t in ev.tickers
    }
    (opp,) = detect(snapshot(ev, books), DetectConfig()).opportunities
    assert all(leg.order.qty == Qty.contracts(10) for leg in opp.basket.legs)


def test_whole_contract_candidates() -> None:
    solution = LpSolution("optimal", 1.0, {("A", Side.NO): 10.6, ("B", Side.NO): 7.3})
    candidates = whole_contract_candidates(solution)
    assert candidates[0] == {("A", Side.NO): Qty.contracts(10), ("B", Side.NO): Qty.contracts(7)}
    assert {("A", Side.NO): Qty.contracts(7), ("B", Side.NO): Qty.contracts(7)} in candidates
    assert {("A", Side.NO): Qty.contracts(1), ("B", Side.NO): Qty.contracts(1)} in candidates
