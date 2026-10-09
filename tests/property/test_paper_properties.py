"""Properties tying paper trading to the outcome model.

1. Settlement agrees with the atoms. For any position and any atom, settling every market the way
   that atom says pays exactly what the atom model says the position pays. So a settlement can only
   fall below a position's guaranteed worst case if the exchange settled in a way the model
   considered impossible -- which is exactly what a model violation reports.
2. Unchanged books execute exactly as planned, and repairing never lowers the worst case when the
   repair executes on the book it was decided on.
"""

from __future__ import annotations

from datetime import timedelta

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.arb.opportunity import LegFill
from karb.arb.verify import payoff_by_atom
from karb.core.fixed import PRICE_SCALE, Price, Qty
from karb.market.book import Level, OrderBook, Side
from karb.market.fees import taker_buy_cost
from karb.paper.settle import MarketResult, settlement_payout
from karb.paper.trade import PaperConfig, TradePlan, complete_trade, plan_trade
from karb.structure.classify import EventStructure, classify_event
from tests.property.test_arbitrage_properties import structured_events
from tests.support import NOW, SERIES, event, market

SLOW = settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SLOW
@given(st.data())
def test_settlement_pays_what_the_atom_model_says(data: st.DataObject) -> None:
    structure: EventStructure = data.draw(structured_events())
    tickers = list(structure.event.tickers)
    legs: list[LegFill] = []
    for _ in range(data.draw(st.integers(min_value=1, max_value=6))):
        ticker = data.draw(st.sampled_from(tickers))
        side = data.draw(st.sampled_from([Side.YES, Side.NO]))
        level = Level(
            Price(data.draw(st.integers(min_value=1, max_value=PRICE_SCALE - 1))),
            Qty(data.draw(st.integers(min_value=1, max_value=100_000))),
        )
        legs.append(
            LegFill(ticker, side, taker_buy_cost([level], structure.fees, DetectConfig().fees))
        )
    held = [(leg.ticker, leg.side, leg.order.qty) for leg in legs]
    for space in structure.spaces.values():
        payoffs = payoff_by_atom(space, tuple(legs))
        for atom in range(space.size):
            results = {
                ticker: MarketResult(
                    ticker,
                    "finalized",
                    "",
                    Price.ONE if atom in space.yes_atoms[ticker] else Price.ZERO,
                    None,
                )
                for ticker in tickers
            }
            assert settlement_payout(held, results) == payoffs[atom]


@SLOW
@given(st.data())
def test_unchanged_books_execute_as_planned(data: st.DataObject) -> None:
    n = data.draw(st.integers(min_value=2, max_value=5))
    bids = data.draw(
        st.lists(st.integers(min_value=1_500, max_value=9_000), min_size=n, max_size=n)
    )
    assume(sum(bids) >= PRICE_SCALE + 2_000)
    ev = event([market(f"M{i}") for i in range(n)], mutually_exclusive=True)
    structure = classify_event(ev, SERIES).structure
    assert structure is not None
    books = {
        ticker: OrderBook(
            ticker,
            (Level(Price(bid), Qty.contracts(data.draw(st.integers(min_value=5, max_value=200)))),),
            (Level(Price(PRICE_SCALE - min(PRICE_SCALE - 1, bid + 300)), Qty.contracts(10)),),
        )
        for bid, ticker in zip(bids, ev.tickers, strict=True)
    }
    snapshot = EventSnapshot(structure, books, frozenset(ev.tickers), NOW)
    found = detect(snapshot, DetectConfig()).opportunities
    assume(found)
    budget = PaperConfig()
    plan = plan_trade(found[0], snapshot, "EV", DetectConfig(), budget)
    assume(isinstance(plan, TradePlan))
    assert isinstance(plan, TradePlan)

    outcome = complete_trade(
        plan,
        decided_at=NOW,
        entry_books=books,
        entry_at=NOW + timedelta(seconds=1),
        hedge_books=books,
        hedge_at=NOW + timedelta(seconds=2),
        tradeable=frozenset(ev.tickers),
        fee_config=DetectConfig().fees,
        max_levels=10,
        hedge=True,
    )
    attribution = outcome.attribution
    assert attribution.after_entry == plan.planned_pnl
    assert attribution.after_hedge >= attribution.after_entry
    assert outcome.entry_cost == plan.planned_cost
