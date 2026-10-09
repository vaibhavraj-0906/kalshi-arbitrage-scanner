"""Pydantic models of the Kalshi REST payloads the scanner reads.

Only the fields the scanner uses are declared; everything else is ignored. Fixed-point
fields stay ``str`` here and are parsed into exact units at the domain boundary
(``karb.market.model``) -- pydantic never sees a price as a number.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

__all__ = [
    "EventResponseWire",
    "EventWire",
    "ExchangeIndexStatusWire",
    "ExchangeStatusWire",
    "MarketOrderbookWire",
    "MarketWire",
    "OrderbookFpWire",
    "OrderbookWire",
    "OrderbooksWire",
    "PriceRangeWire",
    "SeriesWire",
]


def _blank_to_none(value: object) -> object:
    return None if value == "" else value


def _null_to_blank(value: object) -> object:
    return "" if value is None else value


def _to_decimal(value: object) -> object:
    if value is None or value == "":
        return None
    if isinstance(value, float):
        # Only reachable when a caller bypassed load_json. repr() is the shortest string that
        # round-trips, so it recovers the decimal the server actually wrote.
        return Decimal(repr(value))
    return value


OptionalDatetime = Annotated[datetime | None, BeforeValidator(_blank_to_none)]
OptionalStr = Annotated[str | None, BeforeValidator(_blank_to_none)]
OptionalDecimal = Annotated[Decimal | None, BeforeValidator(_to_decimal)]
Text = Annotated[str, BeforeValidator(_null_to_blank)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PriceRangeWire(WireModel):
    start: str
    end: str
    step: str


class MarketWire(WireModel):
    ticker: str
    event_ticker: str
    market_type: Text = "binary"
    status: Text = ""
    title: Text = ""
    subtitle: Text = ""
    yes_sub_title: Text = ""
    no_sub_title: Text = ""
    strike_type: OptionalStr = None
    floor_strike: OptionalDecimal = None
    cap_strike: OptionalDecimal = None
    custom_strike: dict[str, object] | None = None
    """Identifies who a market is about in multi-participant events, e.g. a player's id."""
    yes_bid_dollars: str = "0.0000"
    yes_ask_dollars: str = "0.0000"
    no_bid_dollars: str = "0.0000"
    no_ask_dollars: str = "0.0000"
    yes_bid_size_fp: str = "0.00"
    yes_ask_size_fp: str = "0.00"
    last_price_dollars: str = "0.0000"
    volume_fp: str = "0.00"
    volume_24h_fp: str = "0.00"
    open_interest_fp: str = "0.00"
    liquidity_dollars: str = "0.0000"
    price_level_structure: Text = ""
    price_ranges: list[PriceRangeWire] = Field(default_factory=list)
    open_time: OptionalDatetime = None
    close_time: OptionalDatetime = None
    expected_expiration_time: OptionalDatetime = None
    latest_expiration_time: OptionalDatetime = None
    result: Text = ""
    settlement_value_dollars: OptionalStr = None
    """What one YES contract paid, once the market is determined."""
    settlement_ts: OptionalDatetime = None
    exchange_index: int = 0
    fee_waiver_expiration_time: OptionalDatetime = None
    mve_collection_ticker: OptionalStr = None
    is_provisional: bool | None = None
    rules_primary: Text = ""


class EventWire(WireModel):
    event_ticker: str
    series_ticker: Text = ""
    title: Text = ""
    sub_title: Text = ""
    category: Text = ""
    mutually_exclusive: bool = False
    collateral_return_type: Text = ""
    fee_type_override: OptionalStr = None
    fee_multiplier_override: OptionalDecimal = None
    markets: list[MarketWire] = Field(default_factory=list)


class EventResponseWire(WireModel):
    """``GET /events/{event_ticker}``: markets arrive nested, or as a sibling list."""

    event: EventWire
    markets: list[MarketWire] = Field(default_factory=list)


class SeriesWire(WireModel):
    ticker: str
    title: Text = ""
    category: Text = ""
    fee_type: Text = ""
    fee_multiplier: OptionalDecimal = None


class OrderbookFpWire(WireModel):
    """Bids only, each ``[price_dollars, count_fp]``, ascending by price (best last)."""

    yes_dollars: list[tuple[str, str]] | None = None
    no_dollars: list[tuple[str, str]] | None = None


class OrderbookWire(WireModel):
    """``GET /markets/{ticker}/orderbook``."""

    orderbook_fp: OrderbookFpWire


class MarketOrderbookWire(WireModel):
    ticker: str
    orderbook_fp: OrderbookFpWire


class OrderbooksWire(WireModel):
    """``GET /markets/orderbooks?tickers=...`` (up to 100 tickers per request)."""

    orderbooks: list[MarketOrderbookWire] = Field(default_factory=list)


class ExchangeIndexStatusWire(WireModel):
    exchange_index: int
    description: Text = ""
    exchange_active: bool = False
    trading_active: bool = False


class ExchangeStatusWire(WireModel):
    exchange_active: bool = False
    trading_active: bool = False
    exchange_index_statuses: list[ExchangeIndexStatusWire] = Field(default_factory=list)
