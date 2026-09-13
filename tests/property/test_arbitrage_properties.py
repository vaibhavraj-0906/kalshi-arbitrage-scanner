"""The properties the whole scanner exists to guarantee.

1. No false positives. If some probability distribution over the outcomes prices every market
   within its bid and ask, no basket can guarantee a profit (the finite-state fundamental
   theorem of asset pricing), so the detector must find nothing -- even with fees and rounding
   switched off, where the bound is tight.
2. Planted arbitrage is found.
3. Soundness. Every reported basket pays at least its reported guarantee in every outcome,
   checked by evaluating the markets' strike rules directly -- independently of the atom
   machinery the detector itself relies on.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.arb.opportunity import Opportunity
from karb.core.fixed import PRICE_SCALE, Cash, Price, Qty
from karb.market.book import BookIntegrityError, Level, OrderBook, Side
from karb.market.fees import FeeConfig
from karb.structure.classify import EventStructure, StructureKind, Tier, classify_event
from tests.support import NOW, SERIES, event, market

EXACT = DetectConfig(
    fees=FeeConfig(taker_coefficient=Fraction(0), balance_unit=1),
    min_profit=Cash(1),
    max_contracts_per_leg=None,
)
REALISTIC = DetectConfig()
INTERVAL_TYPES = ["greater", "greater_or_equal", "less", "less_or_equal", "between"]
SLOW = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@st.composite
def structured_events(draw: st.DrawFn) -> EventStructure:
    if draw(st.booleans()):
        markets = [market(f"M{i}") for i in range(draw(st.integers(min_value=2, max_value=6)))]
        asserted = frozenset({"EV"}) if draw(st.booleans()) else frozenset()
        result = classify_event(
            event(markets, mutually_exclusive=True), SERIES, asserted_exhaustive=asserted
        )
    else:
        strikes = sorted(
            draw(st.sets(st.integers(min_value=1, max_value=30), min_size=1, max_size=5))
        )
        markets = []
        for index in range(draw(st.integers(min_value=2, max_value=6))):
            low = draw(st.sampled_from(strikes))
            high = draw(st.sampled_from([s for s in strikes if s >= low]))
            kind = draw(st.sampled_from(INTERVAL_TYPES))
            markets.append(market(f"M{index}", strike_type=kind, floor=str(low), cap=str(high)))
        result = classify_event(event(markets), SERIES)
        # Random strikes often duplicate or touch, which classification rightly refuses. Shed
        # markets until the event is eligible rather than discarding the whole example.
        while result.structure is None and len(markets) > 2:
            markets.pop()
            result = classify_event(event(markets), SERIES)
    assume(result.structure is not None)
    assert result.structure is not None
    return result.structure


def _ladder(draw: st.DrawFn, best: int) -> tuple[Level, ...]:
    levels: list[Level] = []
    price = best
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        if price <= 0:
            break
        levels.append(Level(Price(price), Qty(draw(st.integers(min_value=1, max_value=5_000)))))
        price -= draw(st.integers(min_value=1, max_value=500))
    return tuple(levels)


@st.composite
def consistent_books(draw: st.DrawFn, structure: EventStructure) -> dict[str, OrderBook]:
    """Books whose every bid <= fair <= ask, for fair prices from one distribution.

    The distribution lives on the most assumption-laden space the event has (STRUCTURAL or
    ASSERTED when present), so it puts no mass on outcomes those tiers rule out -- making the
    quotes consistent under every tier at once.
    """
    space = list(structure.spaces.values())[-1]
    weights = draw(
        st.lists(st.integers(min_value=0, max_value=12), min_size=space.size, max_size=space.size)
    )
    if not any(weights):
        weights = [1, *weights[1:]]
    total = sum(weights)
    books: dict[str, OrderBook] = {}
    for ticker in structure.event.tickers:
        fair = Fraction(sum(weights[a] for a in space.yes_atoms[ticker]) * PRICE_SCALE, total)
        bid = math.floor(fair) - draw(st.integers(min_value=0, max_value=400))
        ask = math.ceil(fair) + draw(st.integers(min_value=0, max_value=400))
        books[ticker] = OrderBook(ticker, _ladder(draw, bid), _ladder(draw, PRICE_SCALE - ask))
    return books


def _outcomes(structure: EventStructure, tier: Tier) -> list[Callable[[str], bool]]:
    """Every way the event can settle, as 'does this market resolve YES?' -- no atoms involved."""
    if structure.kind is StructureKind.CATEGORICAL:
        winners: list[str | None] = list(structure.event.tickers)
        if tier is not Tier.ASSERTED:
            winners.append(None)
        return [lambda ticker, w=w: ticker == w for w in winners]
    strikes = sorted({v for interval in structure.intervals.values() for v in interval.endpoints})
    grid = structure.spaces[Tier.STRUCTURAL].epsilon if tier is Tier.STRUCTURAL else None
    step = grid if grid is not None else Decimal("0.001")
    probes = {strikes[0] - 100, strikes[-1] + 100}
    for s in strikes:
        probes |= {s, s - step, s + step, s - step / 2, s + step / 2}
    if grid is not None:  # STRUCTURAL guarantees only hold for settlements on the grid
        probes = {p for p in probes if (p / grid) == (p / grid).to_integral_value()}
    return [lambda ticker, x=x: structure.intervals[ticker].contains(x) for x in probes]


def assert_sound(opportunity: Opportunity, structure: EventStructure) -> None:
    for resolves_yes in _outcomes(structure, opportunity.tier):
        payout = sum(
            leg.order.qty.payout().raw
            for leg in opportunity.basket.legs
            if resolves_yes(leg.ticker) == (leg.side is Side.YES)
        )
        assert payout - opportunity.cost.raw >= opportunity.guaranteed_pnl.raw


@SLOW
@given(st.data())
def test_no_false_positives_when_one_distribution_explains_every_quote(data: st.DataObject) -> None:
    structure = data.draw(structured_events())
    books = data.draw(consistent_books(structure))
    detection = detect(
        EventSnapshot(structure, books, frozenset(structure.event.tickers), NOW), EXACT
    )
    assert detection.opportunities == ()
    for tier, solution in detection.lp.items():
        assert solution.profit <= 1e-6, (tier, solution)


@SLOW
@given(st.data())
def test_planted_overround_is_found_and_sound(data: st.DataObject) -> None:
    n = data.draw(st.integers(min_value=2, max_value=6))
    bids = data.draw(
        st.lists(st.integers(min_value=1_500, max_value=9_000), min_size=n, max_size=n)
    )
    assume(sum(bids) >= PRICE_SCALE + 2_000)
    ev = event([market(f"M{i}") for i in range(n)], mutually_exclusive=True)
    structure = classify_event(ev, SERIES).structure
    assert structure is not None
    books = {}
    for bid, ticker in zip(bids, ev.tickers, strict=True):
        ask = min(PRICE_SCALE - 1, bid + data.draw(st.integers(min_value=1, max_value=800)))
        size = Qty.contracts(data.draw(st.integers(min_value=5, max_value=200)))
        books[ticker] = OrderBook(
            ticker,
            (Level(Price(bid), size),),
            (Level(Price(PRICE_SCALE - ask), Qty.contracts(10)),),
        )
    (opportunity,) = detect(
        EventSnapshot(structure, books, frozenset(ev.tickers), NOW), REALISTIC
    ).opportunities
    assert opportunity.tier is Tier.LOGICAL
    assert opportunity.guaranteed_pnl > Cash.ZERO
    assert_sound(opportunity, structure)


def _shift(book: OrderBook, yes_shift: int, no_shift: int) -> OrderBook:
    def moved(ladder: tuple[Level, ...], shift: int) -> tuple[Level, ...]:
        if not ladder or ladder[0].price.raw + shift >= PRICE_SCALE:
            return ladder
        return tuple(Level(Price(level.price.raw + shift), level.qty) for level in ladder)

    try:
        return OrderBook(
            book.ticker, moved(book.yes_bids, yes_shift), moved(book.no_bids, no_shift)
        )
    except BookIntegrityError:
        return book


@SLOW
@given(st.data())
def test_every_reported_basket_pays_its_guarantee_in_every_outcome(data: st.DataObject) -> None:
    structure = data.draw(structured_events())
    books = data.draw(consistent_books(structure))
    distorted = {
        ticker: _shift(
            book,
            data.draw(st.integers(min_value=0, max_value=3_000)),
            data.draw(st.integers(min_value=0, max_value=3_000)),
        )
        for ticker, book in books.items()
    }
    snapshot = EventSnapshot(structure, distorted, frozenset(structure.event.tickers), NOW)
    for opportunity in detect(snapshot, REALISTIC).opportunities:
        assert_sound(opportunity, structure)
