"""The scanner's view of Kalshi series, events and markets, in exact units.

Wire models (``karb.wire.models``) carry Kalshi's strings; these carry ``Price``, ``Qty`` and
``Cash``. Conversion is strict: a price that is not exact at four decimal places raises
rather than rounds, because a silently rounded quote is a silently wrong arbitrage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from karb.core.fixed import PRICE_SCALE, Cash, Price, Qty
from karb.market.grid import GridError, PriceGrid
from karb.wire.models import EventWire, MarketWire, SeriesWire

__all__ = [
    "EventInfo",
    "MarketInfo",
    "Quote",
    "SeriesInfo",
    "event_from_wire",
    "market_from_wire",
    "series_from_wire",
]


@dataclass(frozen=True, slots=True)
class Quote:
    """Top of book as a market listing reports it, from the YES side.

    Kalshi reports an absent bid *and* an absent ask as a price of zero (a YES ask of $0 would
    be a free contract), so both become ``None``. The NO side is implied: a YES bid at p is a
    NO ask at 1 - p.
    """

    yes_bid: Price | None
    yes_ask: Price | None
    yes_bid_size: Qty
    yes_ask_size: Qty

    @property
    def no_bid(self) -> Price | None:
        return None if self.yes_ask is None else self.yes_ask.complement()

    @property
    def no_ask(self) -> Price | None:
        return None if self.yes_bid is None else self.yes_bid.complement()

    @property
    def is_crossed(self) -> bool:
        return (
            self.yes_bid is not None and self.yes_ask is not None and self.yes_bid >= self.yes_ask
        )


def _present(price: Price) -> Price | None:
    return price if 0 < price.raw < PRICE_SCALE else None


@dataclass(frozen=True, slots=True)
class MarketInfo:
    ticker: str
    event_ticker: str
    title: str
    yes_sub_title: str
    market_type: str
    status: str
    strike_type: str | None
    floor_strike: Decimal | None
    cap_strike: Decimal | None
    quote: Quote
    volume_24h: Qty
    open_interest: Qty
    liquidity: Cash
    grid: PriceGrid | None
    close_time: datetime | None
    latest_expiration_time: datetime | None
    result: str
    exchange_index: int
    is_mve: bool
    integrity_issues: tuple[str, ...] = ()
    participant: tuple[tuple[str, str], ...] = ()
    """Who the market is about, from ``custom_strike`` (e.g. a player's id). Empty if unstated."""

    @property
    def is_binary(self) -> bool:
        return self.market_type == "binary"

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True, slots=True)
class EventInfo:
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str
    category: str
    mutually_exclusive: bool
    collateral_return_type: str
    fee_type_override: str | None
    fee_multiplier_override: Decimal | None
    markets: tuple[MarketInfo, ...]

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(market.ticker for market in self.markets)

    def market(self, ticker: str) -> MarketInfo:
        for market in self.markets:
            if market.ticker == ticker:
                return market
        raise KeyError(ticker)

    def with_listings(self, listings: Mapping[str, MarketInfo]) -> EventInfo:
        """This event with its markets refreshed from a newer market sweep."""
        return replace(self, markets=tuple(listings.get(m.ticker, m) for m in self.markets))


@dataclass(frozen=True, slots=True)
class SeriesInfo:
    ticker: str
    title: str
    category: str
    fee_type: str
    fee_multiplier: Decimal | None


def _participant(custom_strike: Mapping[str, object] | None) -> tuple[tuple[str, str], ...]:
    if not custom_strike:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in custom_strike.items()))


def market_from_wire(wire: MarketWire) -> MarketInfo:
    issues: list[str] = []
    quote = Quote(
        yes_bid=_present(Price.parse(wire.yes_bid_dollars)),
        yes_ask=_present(Price.parse(wire.yes_ask_dollars)),
        yes_bid_size=Qty.parse(wire.yes_bid_size_fp),
        yes_ask_size=Qty.parse(wire.yes_ask_size_fp),
    )
    if quote.is_crossed:
        issues.append(f"crossed listing quote: bid {quote.yes_bid} >= ask {quote.yes_ask}")
    grid: PriceGrid | None = None
    if wire.price_ranges:
        try:
            grid = PriceGrid.from_strings([(r.start, r.end, r.step) for r in wire.price_ranges])
        except GridError as exc:
            issues.append(f"invalid price grid: {exc}")
    return MarketInfo(
        ticker=wire.ticker,
        event_ticker=wire.event_ticker,
        title=wire.title,
        yes_sub_title=wire.yes_sub_title,
        market_type=wire.market_type,
        status=wire.status,
        strike_type=wire.strike_type,
        floor_strike=wire.floor_strike,
        cap_strike=wire.cap_strike,
        quote=quote,
        volume_24h=Qty.parse(wire.volume_24h_fp),
        open_interest=Qty.parse(wire.open_interest_fp),
        liquidity=Cash.parse(wire.liquidity_dollars),
        grid=grid,
        close_time=wire.close_time,
        latest_expiration_time=wire.latest_expiration_time,
        result=wire.result,
        exchange_index=wire.exchange_index,
        is_mve=wire.mve_collection_ticker is not None,
        integrity_issues=tuple(issues),
        participant=_participant(wire.custom_strike),
    )


def event_from_wire(wire: EventWire, sibling_markets: Sequence[MarketWire] = ()) -> EventInfo:
    markets = [*wire.markets, *(m for m in sibling_markets if m.event_ticker == wire.event_ticker)]
    return EventInfo(
        event_ticker=wire.event_ticker,
        series_ticker=wire.series_ticker,
        title=wire.title,
        sub_title=wire.sub_title,
        category=wire.category,
        mutually_exclusive=wire.mutually_exclusive,
        collateral_return_type=wire.collateral_return_type,
        fee_type_override=wire.fee_type_override,
        fee_multiplier_override=wire.fee_multiplier_override,
        markets=tuple(market_from_wire(m) for m in markets),
    )


def series_from_wire(wire: SeriesWire) -> SeriesInfo:
    return SeriesInfo(
        ticker=wire.ticker,
        title=wire.title,
        category=wire.category,
        fee_type=wire.fee_type,
        fee_multiplier=wire.fee_multiplier,
    )
