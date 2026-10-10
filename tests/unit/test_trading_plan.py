"""Planning, repair and attribution on a planted three-way overround, priced by hand.

Decision book, for each of A, B, C: YES bids 0.40 x 50, NO bids 0.55 x 50. So NO costs 0.60
(against the YES bid) and YES costs 0.45 (against the NO bid).

Plan: NO on all three, 50 each. Per leg $30 + fee ceil(0.07 x 50 x 0.6 x 0.4) = $0.84 -> $30.84.
Cost $92.52, guaranteed payout $100 (two of three NOs always pay): planned P&L +$7.48.

Fills here are built the way the exchange reports them -- price, count and trade fee per fill --
so these tests exercise exactly what a live trade attributes.
"""

from __future__ import annotations

from datetime import timedelta
from fractions import Fraction

import pytest

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.core.fixed import Cash, Price, Qty
from karb.market.book import OrderBook, Side
from karb.market.fees import taker_trade_fee
from karb.structure.classify import classify_event
from karb.trading.hedge import decide_hedge
from karb.trading.orders import ExchangeFill, Order, PlacedOrder
from karb.trading.plan import (
    Attribution,
    TradeConfig,
    TradePlan,
    assemble_outcome,
    plan_trade,
    trade_rows,
)
from tests.support import NOW, SERIES, book, event, market

EVENT = event([market("A"), market("B"), market("C")], mutually_exclusive=True)
FULL = {t: book(t, yes=[("0.40", "50")], no=[("0.55", "50")]) for t in "ABC"}
# After NO A and NO B fill, their YES bids are gone; C's were taken by someone else.
AFTER_ENTRY = {t: book(t, yes=[], no=[("0.55", "50")]) for t in "ABC"}
RATE = Fraction(7, 100)


def snapshot(books: dict[str, OrderBook]) -> EventSnapshot:
    structure = classify_event(EVENT, SERIES).structure
    assert structure is not None
    return EventSnapshot(structure, books, frozenset("ABC"), NOW)


def planned(config: TradeConfig | None = None) -> TradePlan:
    decision = snapshot(FULL)
    (opportunity,) = detect(decision, DetectConfig()).opportunities
    plan = plan_trade(opportunity, decision, "EV", DetectConfig(), config or TradeConfig())
    assert isinstance(plan, TradePlan), plan
    return plan


def filled(order: Order, *levels: tuple[str, int], error: str = "") -> PlacedOrder:
    """``order`` as the exchange would report it filling at ``levels`` (price, contracts)."""
    fills = tuple(
        ExchangeFill(
            f"f{i}",
            "o1",
            order.ticker,
            order.side,
            Price.parse(price),
            Qty.contracts(count),
            taker_trade_fee(Price.parse(price), Qty.contracts(count), RATE),
            True,
        )
        for i, (price, count) in enumerate(levels)
    )
    return PlacedOrder(order, f"cid-{order.ticker}", "o1", fills, error)


def exact(plan: TradePlan) -> list[PlacedOrder]:
    return [filled(o, (str(o.limit), o.qty.raw // 100)) for o in plan.orders]


def test_plan_matches_the_detected_basket() -> None:
    plan = planned()
    assert plan.planned_pnl == Cash.parse("7.48")
    assert plan.planned_cost == Cash.parse("92.52")
    assert {(o.ticker, o.side, o.limit, o.qty) for o in plan.orders} == {
        (t, Side.NO, Price.parse("0.60"), Qty.contracts(50)) for t in "ABC"
    }


def test_plan_scales_down_to_the_budget() -> None:
    # $40 / $92.52 of 50 contracts floors to 21. Per leg: $12.60 + fee $0.3528 -> $12.96.
    plan = planned(TradeConfig(max_cost_per_trade=Cash.parse("40")))
    assert {o.qty for o in plan.orders} == {Qty.contracts(21)}
    assert plan.planned_cost == Cash.parse("38.88")
    assert plan.planned_pnl == Cash.parse("3.12")  # $42 guaranteed


def test_plan_refuses_a_budget_too_small_for_one_contract_a_leg() -> None:
    decision = snapshot(FULL)
    (opportunity,) = detect(decision, DetectConfig()).opportunities
    small = TradeConfig(max_cost_per_trade=Cash.parse("1"))
    assert isinstance(plan_trade(opportunity, decision, "EV", DetectConfig(), small), str)


def test_an_exact_fill_reproduces_the_plan() -> None:
    plan = planned()
    outcome = assemble_outcome(
        plan, decided_at=NOW, entry=exact(plan), entry_at=NOW, hedge=(), hedge_at=None
    )
    assert outcome.status == "open"
    assert outcome.attribution == Attribution(*(Cash.parse("7.48"),) * 3)
    assert outcome.entry_cost == plan.planned_cost  # exchange fees plus rounding, to the cent
    assert outcome.netted_cash == Cash.ZERO
    assert outcome.note == ""


def test_a_missing_leg_is_repaired_by_unwinding_the_others() -> None:
    """C's YES bids vanish before the orders land, so NO on C cannot be bought.

    Entry: NO A and NO B fill, $61.68. Worst case: one of A, B wins and only one NO pays $50.
        after entry = $50 - $61.68 = -$11.68.
    Repair, on the books after the entry: buy YES A and YES B at 0.45, 50 each. Per leg $22.50 +
    fee $0.86625 -> $23.37. Every outcome now pays exactly $100 for $108.42.
        after repair = -$8.42: the least bad position the book allows.
    The exchange nets each YES against the NO already held: $100 comes back at once.
    """
    plan = planned()
    by_ticker = {o.ticker: o for o in plan.orders}
    entry = [
        filled(by_ticker["A"], ("0.60", 50)),
        filled(by_ticker["B"], ("0.60", 50)),
        filled(by_ticker["C"]),
    ]
    legs = [p.leg_fill() for p in entry if p.leg_fill() is not None]
    repair = decide_hedge(
        plan.opportunity.space,
        AFTER_ENTRY,
        legs,  # type: ignore[arg-type]
        frozenset("ABC"),
        plan.fees,
        DetectConfig().fees,
        max_levels=10,
    )
    assert {(o.ticker, o.side, o.limit, o.qty) for o in repair} == {
        (t, Side.YES, Price.parse("0.45"), Qty.contracts(50)) for t in "AB"
    }
    hedge = [filled(o, ("0.45", 50)) for o in repair]
    outcome = assemble_outcome(
        plan,
        decided_at=NOW,
        entry=entry,
        entry_at=NOW + timedelta(seconds=1),
        hedge=hedge,
        hedge_at=NOW + timedelta(seconds=2),
    )
    a = outcome.attribution
    assert (a.after_entry, a.after_hedge) == (Cash.parse("-11.68"), Cash.parse("-8.42"))
    assert (a.execution, a.hedging) == (Cash.parse("-19.16"), Cash.parse("3.26"))
    assert outcome.best_after_hedge == a.after_hedge  # the payoff is now constant
    assert outcome.netted_cash == Cash.parse("100")
    assert outcome.filled_contracts == Qty.contracts(100)
    assert "entry filled 67%" in outcome.note and "repaired with 2 order(s)" in outcome.note


def test_without_repair_the_imbalance_is_held() -> None:
    plan = planned()
    by_ticker = {o.ticker: o for o in plan.orders}
    entry = [filled(by_ticker["A"], ("0.60", 50)), filled(by_ticker["B"], ("0.60", 50))]
    outcome = assemble_outcome(
        plan, decided_at=NOW, entry=entry, entry_at=NOW, hedge=(), hedge_at=None
    )
    assert outcome.attribution.after_hedge == Cash.parse("-11.68")
    assert outcome.best_after_hedge == Cash.parse("38.32")  # C or nobody wins: $100 - $61.68


def test_a_full_position_needs_no_repair() -> None:
    plan = planned()
    legs = [p.leg_fill() for p in exact(plan)]
    repair = decide_hedge(
        plan.opportunity.space,
        AFTER_ENTRY,
        legs,  # type: ignore[arg-type]
        frozenset("ABC"),
        plan.fees,
        DetectConfig().fees,
        max_levels=10,
    )
    assert repair == ()


def test_nothing_filled_is_flat_and_errors_are_noted() -> None:
    plan = planned()
    entry = [filled(o, error="order rejected: market closed") for o in plan.orders]
    outcome = assemble_outcome(
        plan, decided_at=NOW, entry=entry, entry_at=NOW, hedge=(), hedge_at=None
    )
    assert outcome.status == "flat"
    assert outcome.attribution == Attribution(Cash.parse("7.48"), Cash.ZERO, Cash.ZERO)
    assert "3 order error(s): order rejected: market closed" in outcome.note


def test_trade_rows_keep_the_exchange_record() -> None:
    plan = planned()
    entry = exact(plan)
    outcome = assemble_outcome(
        plan,
        decided_at=NOW,
        entry=entry,
        entry_at=NOW,
        hedge=(),
        hedge_at=None,
        balance_change=Cash.parse("-92.52"),
    )
    trade, orders = trade_rows(
        outcome,
        trade_id="S-0001",
        session_id="S",
        run_id="R",
        cycle_no=1,
        fee_config=DetectConfig().fees,
    )
    assert (trade.netted_cash, trade.balance_change) == (0, -92_520_000)
    entry_rows = [o for o in orders if o.phase == "entry"]
    assert {o.client_order_id for o in entry_rows} == {"cid-A", "cid-B", "cid-C"}
    assert all(o.order_id == "o1" and o.fees == o.model_fees == 840_000 for o in entry_rows)
    assert [o.phase for o in orders].count("plan") == 3


def test_attribution_telescopes() -> None:
    a = Attribution(Cash(7), Cash(-11), Cash(-8), realized=Cash(-8))
    assert a.planned + a.execution + a.hedging + (a.outcome or Cash.ZERO) == a.realized
    assert not a.model_violation
    assert Attribution(Cash(7), Cash(7), Cash(7), realized=Cash(3)).model_violation


def test_trade_configs_are_validated() -> None:
    with pytest.raises(ValueError):
        TradeConfig(capital=Cash.ZERO)
    with pytest.raises(ValueError):
        TradeConfig(max_trades=0)
    with pytest.raises(ValueError):
        TradeConfig(batch_size=11)  # more than Kalshi's Basic write bucket holds
