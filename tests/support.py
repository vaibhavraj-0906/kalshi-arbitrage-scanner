"""Shared test helpers: golden fixtures, and builders for synthetic events and books."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from karb.core.fixed import Cash, Price, Qty
from karb.market.book import Level, OrderBook
from karb.market.model import (
    EventInfo,
    MarketInfo,
    Quote,
    SeriesInfo,
    event_from_wire,
    series_from_wire,
)
from karb.wire.decode import load_json
from karb.wire.models import EventResponseWire, EventWire, OrderbooksWire, SeriesWire

FIXTURES = Path(__file__).parent / "golden" / "fixtures"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
SERIES = SeriesInfo("SER", "Synthetic series", "Test", "quadratic", Decimal(1))


# --- golden fixtures -------------------------------------------------------------------------


def load_fixture(name: str) -> Any:
    return load_json((FIXTURES / name).read_bytes())


def captured_at() -> datetime:
    return datetime.fromisoformat(load_fixture("manifest.json")["captured_at"])


def fixture_event(name: str) -> EventInfo:
    payload = load_fixture(name)
    if "events" in payload:
        (raw_event,) = payload["events"]
        return event_from_wire(EventWire.model_validate(raw_event))
    response = EventResponseWire.model_validate(payload)
    return event_from_wire(response.event, response.markets)


def fixture_series() -> dict[str, SeriesInfo]:
    wires = (SeriesWire.model_validate(s) for s in load_fixture("series_subset.json")["series"])
    return {wire.ticker: series_from_wire(wire) for wire in wires}


def fixture_books(name: str) -> dict[str, OrderBook]:
    wire = OrderbooksWire.model_validate(load_fixture(name))
    return {
        entry.ticker: OrderBook.from_wire(
            entry.ticker, entry.orderbook_fp.yes_dollars, entry.orderbook_fp.no_dollars
        )
        for entry in wire.orderbooks
    }


# --- synthetic builders ----------------------------------------------------------------------


def lv(price: str, qty: str) -> Level:
    return Level(Price.parse(price), Qty.parse(qty))


def book(
    ticker: str, yes: Sequence[tuple[str, str]] = (), no: Sequence[tuple[str, str]] = ()
) -> OrderBook:
    """An order book from bid ladders written best (highest) first."""
    return OrderBook(ticker, tuple(lv(p, q) for p, q in yes), tuple(lv(p, q) for p, q in no))


def market(
    ticker: str,
    *,
    strike_type: str | None = None,
    floor: str | Decimal | None = None,
    cap: str | Decimal | None = None,
    bid: str | None = None,
    ask: str | None = None,
    status: str = "active",
    market_type: str = "binary",
    result: str = "",
    closes_in: timedelta = timedelta(days=30),
    exchange_index: int = 0,
    is_mve: bool = False,
    event_ticker: str = "EV",
    participant: dict[str, str] | None = None,
) -> MarketInfo:
    return MarketInfo(
        ticker=ticker,
        event_ticker=event_ticker,
        title=ticker,
        yes_sub_title="",
        market_type=market_type,
        status=status,
        strike_type=strike_type,
        floor_strike=None if floor is None else Decimal(floor),
        cap_strike=None if cap is None else Decimal(cap),
        quote=Quote(
            yes_bid=None if bid is None else Price.parse(bid),
            yes_ask=None if ask is None else Price.parse(ask),
            yes_bid_size=Qty.contracts(10),
            yes_ask_size=Qty.contracts(10),
        ),
        volume_24h=Qty.ZERO,
        open_interest=Qty.ZERO,
        liquidity=Cash.ZERO,
        grid=None,
        close_time=NOW + closes_in,
        latest_expiration_time=NOW + closes_in,
        result=result,
        exchange_index=exchange_index,
        is_mve=is_mve,
        participant=tuple(sorted(participant.items())) if participant else (),
    )


def event(
    markets: Sequence[MarketInfo],
    *,
    ticker: str = "EV",
    mutually_exclusive: bool = False,
    series: str = "SER",
    collateral: str = "",
) -> EventInfo:
    return EventInfo(
        event_ticker=ticker,
        series_ticker=series,
        title=f"Synthetic {ticker}",
        sub_title="",
        category="Test",
        mutually_exclusive=mutually_exclusive,
        collateral_return_type=collateral,
        fee_type_override=None,
        fee_multiplier_override=None,
        markets=tuple(markets),
    )
