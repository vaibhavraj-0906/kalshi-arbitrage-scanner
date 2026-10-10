"""The post-trade audit, and the complete sets ``karb trade --exercise`` buys."""

from __future__ import annotations

from karb.arb.detect import DetectConfig, EventSnapshot
from karb.core.fixed import Cash, Qty
from karb.market.book import Side
from karb.structure.classify import classify_event
from karb.trading.engine import audit_trade
from karb.trading.exercise import complete_set, exact_cover
from karb.trading.plan import TradeOutcome, TradePlan, assemble_outcome
from tests.support import NOW, SERIES, book, event, market
from tests.unit.test_trading_plan import exact, filled, planned

HELD = {"A": -5000, "B": -5000, "C": -5000}  # 50 NO on each


def full_fill(balance_change: str = "-92.52") -> tuple[TradeOutcome, Cash]:
    plan = planned()
    entry = exact(plan)
    outcome = assemble_outcome(
        plan,
        decided_at=NOW,
        entry=entry,
        entry_at=NOW,
        hedge=(),
        hedge_at=None,
        balance_change=Cash.parse(balance_change),
    )
    model = Cash.total(p.model_fees(plan.fees, DetectConfig().fees) for p in entry)
    return outcome, model


def test_a_clean_trade_passes_the_audit() -> None:
    outcome, model = full_fill()
    assert audit_trade(outcome, HELD, model) == []


def test_positions_must_match_the_fills() -> None:
    outcome, model = full_fill()
    problems = audit_trade(outcome, {**HELD, "B": -4000}, model)
    assert problems == ["B: the exchange shows a position of -40, the fills -50"]


def test_a_balance_that_fell_further_than_the_fees_explain_is_flagged() -> None:
    # Eight cents more left the account than the fills, fees and rounding account for.
    outcome, model = full_fill("-92.60")
    (problem,) = audit_trade(outcome, HELD, model)
    assert "charged $2.600000" in problem and "allowed $2.520000" in problem


def test_unresolved_order_errors_are_flagged() -> None:
    plan = planned()
    entry = [filled(order, error="no result for this order") for order in plan.orders]
    outcome = assemble_outcome(
        plan, decided_at=NOW, entry=entry, entry_at=NOW, hedge=(), hedge_at=None
    )
    problems = audit_trade(outcome, {}, Cash.ZERO)
    assert problems == [f"{t}: no result for this order" for t in "ABC"]


BRACKETS = event(
    [
        market("LOW", strike_type="less", cap="10"),
        market("MID", strike_type="between", floor="10", cap="19.99"),
        market("HIGH", strike_type="greater", floor="19.99"),
    ],
    mutually_exclusive=True,
)
BRACKET_BOOKS = {
    t: book(t, yes=[("0.30", "40")], no=[("0.64", "40")]) for t in ("LOW", "MID", "HIGH")
}


def bracket_snapshot() -> EventSnapshot:
    structure = classify_event(BRACKETS, SERIES).structure
    assert structure is not None
    return EventSnapshot(structure, BRACKET_BOOKS, frozenset(BRACKET_BOOKS), NOW)


def test_a_bracket_ladder_has_a_complete_set() -> None:
    """One YES on each bracket pays exactly $1 whatever happens.

    Each costs $0.36 + fee ceil(0.07 x 0.36 x 0.64) = $0.016128, aligned to $0.38. Three cost
    $1.14 for a guaranteed $1.00: a known $0.14 loss, the price of watching the path work.
    """
    plan = complete_set(bracket_snapshot(), sets=1, fee_config=DetectConfig().fees)
    assert isinstance(plan, TradePlan), plan
    assert {(o.ticker, o.side, o.qty) for o in plan.orders} == {
        (t, Side.YES, Qty.contracts(1)) for t in ("LOW", "MID", "HIGH")
    }
    assert set(plan.basket.payoffs) == {Cash.parse("1")}
    assert (plan.planned_cost, plan.planned_pnl) == (Cash.parse("1.14"), Cash.parse("-0.14"))


def test_complete_sets_need_depth_and_fall_back_to_one_market() -> None:
    snapshot = bracket_snapshot()
    short = complete_set(snapshot, sets=41, fee_config=DetectConfig().fees)
    assert isinstance(short, str)
    assert "too little YES on HIGH, LOW, MID" in short and "for 41 contract(s)" in short
    assert isinstance(complete_set(snapshot, sets=0, fee_config=DetectConfig().fees), str)

    # A threshold ladder has no set of brackets, but YES and NO on one rung is a complete set.
    thresholds = event(
        [
            market("T10", strike_type="greater", floor="10"),
            market("T20", strike_type="greater", floor="20"),
        ]
    )
    structure = classify_event(thresholds, SERIES).structure
    assert structure is not None
    books = {t: book(t, yes=[("0.40", "10")], no=[("0.50", "10")]) for t in ("T10", "T20")}
    plan = complete_set(
        EventSnapshot(structure, books, frozenset(books), NOW),
        sets=1,
        fee_config=DetectConfig().fees,
    )
    assert isinstance(plan, TradePlan), plan
    assert {(o.ticker, o.side) for o in plan.orders} == {("T10", Side.YES), ("T10", Side.NO)}


def test_exact_cover_finds_a_partition_among_overlapping_markets() -> None:
    structure = classify_event(BRACKETS, SERIES).structure
    assert structure is not None
    space = next(iter(structure.spaces.values()))
    cover = exact_cover(space, {"LOW", "MID", "HIGH"})
    assert cover is not None and sorted(cover) == ["HIGH", "LOW", "MID"]
    assert exact_cover(space, {"LOW", "HIGH"}) is None


def test_without_a_full_ladder_the_exercise_buys_both_sides_of_one_market() -> None:
    books = {
        **BRACKET_BOOKS,
        "LOW": book("LOW", yes=[("0.30", "40")], no=[]),  # no YES ask: the ladder is incomplete
    }
    structure = classify_event(BRACKETS, SERIES).structure
    assert structure is not None
    snapshot = EventSnapshot(structure, books, frozenset(books), NOW)
    plan = complete_set(snapshot, sets=2, fee_config=DetectConfig().fees)
    assert isinstance(plan, TradePlan), plan
    # MID and HIGH quote both sides at the same prices; the first in order wins the tie.
    assert {(o.ticker, o.side) for o in plan.orders} == {("HIGH", Side.YES), ("HIGH", Side.NO)}
    assert set(plan.basket.payoffs) == {Cash.parse("2")}
    assert plan.opportunity.kind == "EXERCISE"

    bare = {t: book(t, yes=[("0.30", "40")], no=[]) for t in BRACKET_BOOKS}
    nothing = complete_set(
        EventSnapshot(structure, bare, frozenset(bare), NOW), sets=1, fee_config=DetectConfig().fees
    )
    assert isinstance(nothing, str) and "no market quotes both YES and NO" in nothing
