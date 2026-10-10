"""The order wire format, and fills priced exactly from what the exchange reports."""

from __future__ import annotations

import uuid
from fractions import Fraction

import pytest

from karb.core.fixed import Cash, FixedPointError, Price, Qty
from karb.market.book import Side
from karb.market.fees import CENTICENT_BALANCE_UNIT, FeeConfig, FeeSchedule, FeeType
from karb.trading.orders import (
    ExchangeFill,
    Order,
    PlacedOrder,
    client_order_id,
    order_request,
    parse_fill,
    parse_wire_price,
    walk,
)
from tests.support import book

QUADRATIC = FeeSchedule(FeeType.QUADRATIC, Fraction(1), "series")


def test_yes_is_a_bid_and_no_is_an_ask_at_the_complement() -> None:
    yes = order_request(Order("T", Side.YES, Price.parse("0.45"), Qty.contracts(50)), "cid")
    assert yes == {
        "ticker": "T",
        "side": "bid",
        "count": "50.00",
        "price": "0.4500",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": "cid",
    }
    # Buying NO for at most $0.60 is selling YES for at least $0.40.
    no = order_request(Order("T", Side.NO, Price.parse("0.60"), Qty.parse("2.5")), "cid")
    assert (no["side"], no["price"], no["count"]) == ("ask", "0.4000", "2.50")


def test_client_order_ids_are_stable_and_distinct() -> None:
    first = client_order_id("trade-1", "entry", 0)
    assert first == client_order_id("trade-1", "entry", 0)
    assert uuid.UUID(first).version == 5
    others = {client_order_id("trade-1", "entry", 1), client_order_id("trade-1", "hedge", 0)}
    assert first not in others and len(others) == 2


def test_wire_prices_may_be_padded_but_never_finer_than_the_grid() -> None:
    assert parse_wire_price("0.450000") == Price.parse("0.45")
    assert parse_wire_price("1") == Price.ONE
    with pytest.raises(FixedPointError):
        parse_wire_price("0.450001")
    with pytest.raises(FixedPointError):
        parse_wire_price("cheap")


def test_fills_are_priced_on_the_side_bought() -> None:
    raw = {
        "fill_id": "f1",
        "order_id": "o1",
        "ticker": "T",
        "outcome_side": "no",
        "count_fp": "30.00",
        "yes_price_dollars": "0.600000",
        "no_price_dollars": "0.400000",
        "fee_cost": "0.504000",
        "is_taker": True,
    }
    fill = parse_fill(raw)
    assert (fill.side, fill.price, fill.qty, fill.fee) == (
        Side.NO,
        Price.parse("0.40"),
        Qty.contracts(30),
        Cash.parse("0.504"),
    )
    assert parse_fill({**raw, "outcome_side": "yes"}).price == Price.parse("0.60")


def placed(fee: str, *, unit: int | None = None) -> PlacedOrder:
    fill = ExchangeFill(
        "f1", "o1", "T", Side.YES, Price.parse("0.50"), Qty.contracts(30), Cash.parse(fee), True
    )
    order = Order("T", Side.YES, Price.parse("0.50"), Qty.contracts(30))
    if unit is None:
        return PlacedOrder(order, "cid", "o1", (fill,))
    return PlacedOrder(order, "cid", "o1", (fill,), balance_unit=unit)


def test_exchange_fills_add_the_documented_balance_rounding() -> None:
    """30 YES at 0.50: notional $15, fee ceil(0.07 x 30 x 0.5 x 0.5) = $0.525.

    The balance moves in whole cents, so the fill costs $15.53: a $0.005 rounding fee.
    """
    order = placed("0.525")
    leg = order.leg_fill()
    assert leg is not None
    (cost,) = leg.order.fills
    assert (cost.trade_fee, cost.rounding_fee) == (Cash.parse("0.525"), Cash.parse("0.005"))
    assert order.cash_out == Cash.parse("15.53")
    assert (order.exchange_fees, order.fees) == (Cash.parse("0.525"), Cash.parse("0.53"))
    # karb's fee model on the same fill agrees to the micro-dollar.
    assert order.model_fees(QUADRATIC, FeeConfig()) == Cash.parse("0.53")
    # A direct member's balance moves in $0.0001, so nothing rounds here.
    assert placed("0.525", unit=CENTICENT_BALANCE_UNIT).cash_out == Cash.parse("15.525")


def test_a_higher_exchange_fee_shows_against_the_model() -> None:
    assert placed("1.05").fees > placed("1.05").model_fees(QUADRATIC, FeeConfig())


def test_an_unfilled_order_costs_nothing() -> None:
    nothing = PlacedOrder(Order("T", Side.NO, Price.ONE, Qty.contracts(1)), "cid", "o1")
    assert nothing.leg_fill() is None
    assert (nothing.filled, nothing.cash_out, nothing.fees) == (Qty.ZERO, Cash.ZERO, Cash.ZERO)
    assert nothing.model_fees(QUADRATIC, FeeConfig()) == Cash.ZERO
    assert nothing.response_json() == ""


def test_walking_a_book_stops_at_the_limit() -> None:
    deep = book("A", yes=[("0.40", "10"), ("0.30", "10")])  # NO asks 0.60 x 10, 0.70 x 10
    taken = walk(deep, Side.NO, Qty.contracts(20), Price.parse("0.60"))
    assert [(level.price, level.qty) for level in taken] == [
        (Price.parse("0.60"), Qty.contracts(10))
    ]
    everything = walk(deep, Side.NO, Qty.contracts(15), None)
    assert [level.qty for level in everything] == [Qty.contracts(10), Qty.contracts(5)]
