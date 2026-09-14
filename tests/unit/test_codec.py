from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from karb.arb.detect import DetectConfig
from karb.core.fixed import PRICE_SCALE, Cash, Price, Qty
from karb.market.book import Level, OrderBook
from karb.market.fees import FeeConfig, RoundingMode
from karb.market.model import event_from_wire
from karb.store.codec import (
    book_from_row,
    book_to_row,
    detect_config_from_json,
    detect_config_to_json,
    dumps_exact,
    from_ns,
    payload_hash,
    structural_payload,
    to_ns,
)
from karb.structure.classify import Tier, classify_event
from karb.wire.decode import load_json
from karb.wire.models import EventWire
from tests.support import fixture_series, load_fixture


def kxinx_raw() -> dict[str, object]:
    raw: dict[str, object] = load_fixture("events_KXINX.json")["events"][0]
    return raw


def test_exact_json_keeps_every_digit() -> None:
    payload = {
        "floor_strike": Decimal("7249.9999"),
        "cap_strike": Decimal("67599.99"),
        "count": 7225,
        "huge": Decimal("1E+400"),
        "nested": [Decimal("1.50"), {"x": Decimal("0.1")}],
    }
    back = load_json(dumps_exact(payload))
    assert back["floor_strike"] == Decimal("7249.9999")
    assert type(back["floor_strike"]) is Decimal
    assert back["count"] == 7225 and type(back["count"]) is int
    assert back["huge"] == "1E+400"  # beyond a float: kept as text, parsed back by the wire models
    assert back["nested"] == [Decimal("1.5"), {"x": Decimal("0.1")}]


@given(
    st.decimals(
        min_value=Decimal(-(10**9)),
        max_value=Decimal(10**9),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_any_four_place_decimal_survives(value: Decimal) -> None:
    back = load_json(dumps_exact({"v": value}))["v"]
    assert Decimal(back) == value


def test_structural_payload_ignores_quotes_and_market_order() -> None:
    event = kxinx_raw()
    markets = event["markets"]
    assert isinstance(markets, list)
    digest = payload_hash(structural_payload(event))

    shuffled = {**event, "markets": list(reversed(markets))}
    requoted = {
        **event,
        "last_updated_ts": "later",
        "markets": [{**m, "yes_bid_dollars": "0.4200", "volume_24h_fp": "99.00"} for m in markets],
    }
    restruck = {**event, "markets": [{**markets[0], "cap_strike": Decimal("7300")}, *markets[1:]]}

    assert payload_hash(structural_payload(shuffled)) == digest
    assert payload_hash(structural_payload(requoted)) == digest
    assert payload_hash(structural_payload(restruck)) != digest


def test_a_recorded_payload_classifies_like_the_original() -> None:
    stored = load_json(dumps_exact(structural_payload(kxinx_raw())))
    event = event_from_wire(EventWire.model_validate(stored))
    structure = classify_event(event, fixture_series()["KXINX"]).structure
    assert structure is not None
    assert list(structure.spaces) == [Tier.LOGICAL, Tier.STRUCTURAL]


@st.composite
def books(draw: st.DrawFn) -> OrderBook:
    def ladder() -> tuple[Level, ...]:
        prices = sorted(
            draw(st.sets(st.integers(min_value=1, max_value=PRICE_SCALE - 1), max_size=8)),
            reverse=True,
        )
        return tuple(
            Level(Price(price), Qty(draw(st.integers(min_value=1, max_value=10**9))))
            for price in prices
        )

    return OrderBook("T", ladder(), ladder())


@given(books())
def test_book_rows_round_trip(book: OrderBook) -> None:
    assert book_from_row(book.ticker, *book_to_row(book)) == book


def test_detect_config_round_trips_through_json() -> None:
    config = DetectConfig(
        fees=FeeConfig(
            taker_coefficient=Fraction(7, 100),
            balance_unit=100,
            rounding_mode=RoundingMode.ACCUMULATOR,
        ),
        min_profit=Cash(20_000),
        max_levels=7,
        max_contracts_per_leg=None,
        max_cost=Cash(5_000_000),
    )
    encoded = json.loads(json.dumps(detect_config_to_json(config)))
    assert detect_config_from_json(encoded) == config


def test_nanosecond_timestamps_are_exact() -> None:
    moment = datetime(2026, 9, 13, 7, 20, 36, 123456, tzinfo=UTC)
    assert to_ns(moment) == 1_789_284_036_123_456_000
    assert from_ns(to_ns(moment)) == moment
    with pytest.raises(ValueError):
        to_ns(datetime(2026, 9, 13))
