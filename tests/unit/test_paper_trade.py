"""Paper execution on a planted three-way overround, priced by hand.

Decision book, for each of A, B, C: YES bids 0.40 x 50, NO bids 0.55 x 50. So NO costs 0.60
(against the YES bid) and YES costs 0.45 (against the NO bid).

Plan: NO on all three, 50 each. Per leg $30 + fee ceil(0.07 x 50 x 0.6 x 0.4) = $0.84 -> $30.84.
Cost $92.52, guaranteed payout $100 (two of three NOs always pay): planned P&L +$7.48.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.core.fixed import Cash, Price, Qty
from karb.market.book import OrderBook, Side
from karb.paper.execution import Liquidity, Order
from karb.paper.trade import Attribution, PaperConfig, TradePlan, complete_trade, plan_trade
from karb.structure.classify import classify_event
from tests.support import NOW, SERIES, book, event, market

EVENT = event([market("A"), market("B"), market("C")], mutually_exclusive=True)
FULL = {t: book(t, yes=[("0.40", "50")], no=[("0.55", "50")]) for t in "ABC"}


def snapshot(books: dict[str, OrderBook]) -> EventSnapshot:
    structure = classify_event(EVENT, SERIES).structure
    assert structure is not None
    return EventSnapshot(structure, books, frozenset("ABC"), NOW)


def planned(config: PaperConfig | None = None) -> tuple[TradePlan, EventSnapshot]:
    decision = snapshot(FULL)
    (opportunity,) = detect(decision, DetectConfig()).opportunities
    plan = plan_trade(opportunity, decision, "EV", DetectConfig(), config or PaperConfig())
    assert isinstance(plan, TradePlan), plan
    return plan, decision


def run(plan: TradePlan, entry, hedge, *, hedging: bool = True):  # type: ignore[no-untyped-def]
    return complete_trade(
        plan,
        decided_at=NOW,
        entry_books=entry,
        entry_at=None if entry is None else NOW + timedelta(seconds=1),
        hedge_books=hedge,
        hedge_at=None if hedge is None else NOW + timedelta(seconds=2),
        tradeable=frozenset("ABC"),
        fee_config=DetectConfig().fees,
        max_levels=10,
        hedge=hedging,
    )


def test_plan_matches_the_detected_basket() -> None:
    plan, _ = planned()
    assert plan.planned_pnl == Cash.parse("7.48")
    assert plan.planned_cost == Cash.parse("92.52")
    assert {(o.ticker, o.side, o.limit, o.qty) for o in plan.orders} == {
        (t, Side.NO, Price.parse("0.60"), Qty.contracts(50)) for t in "ABC"
    }


def test_plan_scales_down_to_the_budget() -> None:
    # $40 / $92.52 of 50 contracts floors to 21. Per leg: $12.60 + fee $0.3528 -> $12.96.
    plan, _ = planned(PaperConfig(max_cost_per_trade=Cash.parse("40")))
    assert {o.qty for o in plan.orders} == {Qty.contracts(21)}
    assert plan.planned_cost == Cash.parse("38.88")
    assert plan.planned_pnl == Cash.parse("3.12")  # $42 guaranteed


def test_plan_refuses_a_budget_too_small_for_one_contract_a_leg() -> None:
    decision = snapshot(FULL)
    (opportunity,) = detect(decision, DetectConfig()).opportunities
    small = PaperConfig(max_cost_per_trade=Cash.parse("1"))
    assert isinstance(plan_trade(opportunity, decision, "EV", DetectConfig(), small), str)


def test_an_unchanged_book_reproduces_the_plan_exactly() -> None:
    plan, _ = planned()
    outcome = run(plan, FULL, FULL)
    assert outcome.status == "open"
    assert outcome.attribution == Attribution(
        Cash.parse("7.48"), Cash.parse("7.48"), Cash.parse("7.48")
    )
    assert outcome.hedge == ()  # nothing left to improve
    assert outcome.entry_cost == plan.planned_cost


def test_a_missing_leg_is_repaired_by_unwinding_the_others() -> None:
    """C's YES bids vanish before arrival, so NO on C cannot be bought.

    Entry: NO A and NO B fill, $61.68. Worst case: one of A, B wins and only one NO pays $50.
        after entry = $50 - $61.68 = -$11.68.
    Repair, decided on that book: buy YES A and YES B at 0.45, 50 each. Per leg $22.50 +
    fee $0.86625 -> $23.37. Every outcome now pays exactly $100 for $108.42.
        after hedge = -$8.42: the least bad position the book allows.
    """
    plan, _ = planned()
    thin = {**FULL, "C": book("C", yes=[], no=[("0.55", "50")])}
    outcome = run(plan, thin, thin)
    attribution = outcome.attribution
    assert attribution.after_entry == Cash.parse("-11.68")
    assert attribution.after_hedge == Cash.parse("-8.42")
    assert attribution.execution == Cash.parse("-19.16")
    assert attribution.hedging == Cash.parse("3.26")
    assert {(e.order.ticker, e.order.side, e.filled) for e in outcome.hedge} == {
        ("A", Side.YES, Qty.contracts(50)),
        ("B", Side.YES, Qty.contracts(50)),
    }
    assert outcome.best_after_hedge == attribution.after_hedge  # payoff is now constant
    assert outcome.filled_contracts == Qty.contracts(100)
    assert "entry filled 67%" in outcome.note


def test_without_hedging_the_imbalance_is_held() -> None:
    plan, _ = planned()
    thin = {**FULL, "C": book("C", yes=[], no=[("0.55", "50")])}
    outcome = run(plan, thin, thin, hedging=False)
    assert outcome.hedge == ()
    assert outcome.attribution.after_hedge == Cash.parse("-11.68")
    assert outcome.best_after_hedge == Cash.parse("38.32")  # C or nobody wins: $100 - $61.68


def test_a_hedge_with_no_later_book_is_left_unhedged() -> None:
    plan, _ = planned()
    thin = {**FULL, "C": book("C", yes=[], no=[("0.55", "50")])}
    outcome = run(plan, thin, None)
    assert outcome.hedge == ()
    assert "no later book" in outcome.note


def test_nothing_filled_is_flat_and_no_book_is_missed() -> None:
    plan, _ = planned()
    gone = {t: book(t, yes=[], no=[("0.55", "50")]) for t in "ABC"}
    flat = run(plan, gone, gone)
    assert flat.status == "flat"
    assert flat.attribution == Attribution(Cash.parse("7.48"), Cash.ZERO, Cash.ZERO)

    missed = run(plan, None, None)
    assert missed.status == "missed"
    assert missed.attribution.execution == Cash.parse("-7.48")


def test_liquidity_once_taken_stays_taken() -> None:
    liquidity = Liquidity()
    a = FULL["A"]
    first = liquidity.take(a, Side.NO, Qty.contracts(30), Price.parse("0.60"))
    assert [(lv.price, lv.qty) for lv in first] == [(Price.parse("0.60"), Qty.contracts(30))]
    # The same book, or a later snapshot still showing that size, has only 20 left for us.
    again = liquidity.take(a, Side.NO, Qty.contracts(30), Price.parse("0.60"))
    assert [lv.qty for lv in again] == [Qty.contracts(20)]
    assert liquidity.asks(a, Side.NO) == ()
    assert liquidity.asks(a, Side.YES) != ()  # the other side is untouched


def test_limit_prices_cap_what_an_order_will_pay() -> None:
    deep = book("A", yes=[("0.40", "10"), ("0.30", "10")])
    fills = Liquidity().take(deep, Side.NO, Qty.contracts(20), Price.parse("0.60"))
    assert [(lv.price, lv.qty) for lv in fills] == [(Price.parse("0.60"), Qty.contracts(10))]


def test_attribution_telescopes() -> None:
    a = Attribution(Cash(7), Cash(-11), Cash(-8), realized=Cash(-8))
    assert a.planned + a.execution + a.hedging + (a.outcome or Cash.ZERO) == a.realized
    assert not a.model_violation
    assert Attribution(Cash(7), Cash(7), Cash(7), realized=Cash(3)).model_violation


def test_orders_reject_invalid_paper_configs() -> None:
    with pytest.raises(ValueError):
        PaperConfig(latency=-1)
    with pytest.raises(ValueError):
        PaperConfig(capital=Cash.ZERO)
    assert Order("A", Side.NO, Price.ONE, Qty.contracts(1)).qty == Qty.contracts(1)
