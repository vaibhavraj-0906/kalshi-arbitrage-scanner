"""Properties tying trading to the outcome model and to the exchange's own account.

1. Settlement agrees with the atoms. For any position and any atom, settling every market the way
   that atom says pays exactly what the atom model says the position pays. So a settlement can only
   fall below a position's guaranteed worst case if the exchange settled in a way the model
   considered impossible -- which is exactly what a model violation reports.
2. Orders sent to a simulated exchange holding the decision book fill exactly as planned, and the
   exchange's balance and positions reconcile with the attribution to the micro-dollar.
3. When some legs come up short, the repair -- executed on the book it was decided on -- never
   lowers the worst case, and the account still reconciles.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from fractions import Fraction

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.arb.opportunity import LegFill
from karb.arb.verify import payoff_by_atom
from karb.core.clock import FakeClock
from karb.core.fixed import PRICE_SCALE, Cash, Price, Qty
from karb.exchange.client import KalshiClient
from karb.market.book import Level, OrderBook, Side
from karb.market.fees import taker_buy_cost
from karb.structure.classify import EventStructure, classify_event
from karb.trading.auth import Credentials
from karb.trading.engine import audit_trade
from karb.trading.hedge import decide_hedge
from karb.trading.orders import Order, PlacedOrder, position_legs
from karb.trading.plan import TradeConfig, TradeOutcome, TradePlan, assemble_outcome, plan_trade
from karb.trading.portfolio import fetch_balance, fetch_positions, place_orders
from karb.trading.settle import MarketResult, settlement_payout
from karb.trading.simulator import SimulatedDesk
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


CREDENTIALS = Credentials("prop", Ed25519PrivateKey.generate())
RATE = Fraction(7, 100)


def overround(data: st.DataObject) -> tuple[EventSnapshot, TradePlan, dict[str, OrderBook]]:
    """A mutually exclusive event whose YES bids sum well above $1, and the plan to trade it."""
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
    plan = plan_trade(found[0], snapshot, "EV", DetectConfig(), TradeConfig())
    assume(isinstance(plan, TradePlan))
    assert isinstance(plan, TradePlan)
    return snapshot, plan, books


def ladders(books: dict[str, OrderBook]) -> dict[str, dict[str, list[list[str]]]]:
    """Order books in Kalshi's wire shape: bid ladders, best last."""

    def side(levels: Sequence[Level]) -> list[list[str]]:
        return [[str(level.price), str(level.qty)] for level in reversed(levels)]

    return {
        t: {"yes_dollars": side(b.bids(Side.YES)), "no_dollars": side(b.bids(Side.NO))}
        for t, b in books.items()
    }


def books_of(desk: SimulatedDesk) -> dict[str, OrderBook]:
    def side(rows: list[list[str]]) -> tuple[Level, ...]:
        return tuple(Level(Price.parse(p), Qty.parse(q)) for p, q in reversed(rows))

    return {
        t: OrderBook(t, side(ladder.get("yes_dollars") or []), side(ladder.get("no_dollars") or []))
        for t, ladder in desk.ladders.items()
    }


def client_for(desk: SimulatedDesk) -> KalshiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return desk.handle(request, request.url.path.split("/trade-api/v2", 1)[1])

    clock = desk.clock
    assert isinstance(clock, FakeClock)

    async def sleep(seconds: float) -> None:  # the rate limiter waits on the fake clock
        clock.advance(seconds)
        await asyncio.sleep(0)

    return KalshiClient(
        base_url="https://desk.test/trade-api/v2",
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=sleep,
        credentials=CREDENTIALS,
        sign_hosts=frozenset({"desk.test"}),
    )


def desk_for(books: dict[str, OrderBook]) -> SimulatedDesk:
    return SimulatedDesk(
        ladders=ladders(books),
        clock=FakeClock(NOW),
        fee_rate=lambda _ticker: RATE,
        public_key=CREDENTIALS.public_key(),
        key_id=CREDENTIALS.key_id,
        event_of=lambda _ticker: "EV",
    )


async def execute(
    desk: SimulatedDesk, plan: TradePlan, snapshot: EventSnapshot
) -> tuple[TradeOutcome, list[str]]:
    """The engine's lifecycle, minus the scanner: entry, repair, reconciliation, audit."""
    async with client_for(desk) as client:
        before = await fetch_balance(client)
        since = NOW - timedelta(seconds=5)
        entry = await place_orders(client, plan.orders, trade_id="T", phase="entry", since=since)
        hedge: tuple[PlacedOrder, ...] = ()
        if any(p.filled < p.order.qty for p in entry) and position_legs(entry):
            repair: tuple[Order, ...] = decide_hedge(
                plan.opportunity.space,
                books_of(desk),
                position_legs(entry),
                snapshot.tradeable,
                plan.fees,
                DetectConfig().fees,
                max_levels=10,
            )
            if repair:
                hedge = await place_orders(client, repair, trade_id="T", phase="hedge", since=since)
        after = await fetch_balance(client)
        positions = await fetch_positions(client, "EV")
    outcome = assemble_outcome(
        plan,
        decided_at=NOW,
        entry=entry,
        entry_at=NOW,
        hedge=hedge,
        hedge_at=NOW,
        balance_change=after - before,
    )
    model = Cash.total(p.model_fees(plan.fees, DetectConfig().fees) for p in outcome.orders)
    return outcome, audit_trade(outcome, positions, model)


@SLOW
@given(st.data())
def test_orders_on_the_decision_book_fill_as_planned_and_reconcile(data: st.DataObject) -> None:
    snapshot, plan, books = overround(data)
    desk = desk_for(books)
    outcome, problems = asyncio.run(execute(desk, plan, snapshot))
    assert problems == []
    assert outcome.attribution.after_entry == plan.planned_pnl
    assert outcome.entry_cost == plan.planned_cost
    assert outcome.hedge == ()
    # The balance moved by exactly the cost: fees and rounding included, nothing unexplained.
    assert outcome.balance_change == -plan.planned_cost


@SLOW
@given(st.data())
def test_a_repair_on_its_own_book_never_lowers_the_worst_case(data: st.DataObject) -> None:
    snapshot, plan, books = overround(data)
    desk = desk_for(books)
    # Before the orders land, someone takes part or all of the size on some legs.
    shorted = data.draw(st.sets(st.sampled_from(sorted(books)), min_size=1))
    left = {t: data.draw(st.integers(min_value=0, max_value=4)) for t in shorted}

    def race(_ticker: str) -> None:
        for ticker, contracts in left.items():
            rows = desk.ladders[ticker]["yes_dollars"]
            desk.ladders[ticker]["yes_dollars"] = (
                [[rows[-1][0], f"{contracts}.00"]] if rows and contracts else []
            )
        left.clear()

    desk.on_order = race
    outcome, problems = asyncio.run(execute(desk, plan, snapshot))
    assert problems == []
    a = outcome.attribution
    assert a.after_hedge >= a.after_entry
    expected = outcome.netted_cash - outcome.entry_cost - outcome.hedge_cost
    assert outcome.balance_change == expected
