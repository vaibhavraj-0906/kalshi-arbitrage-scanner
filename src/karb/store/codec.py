"""Exact serialisation for recordings.

Two jobs. First, JSON that never loses a digit: Kalshi sends strikes such as ``7249.9999`` as
JSON numbers, decoded as ``Decimal``, and a recording must give back exactly what it was given.
Second, a canonical *structural* form of an event: the exchange's own payload minus the fields
that change every second, so an unchanged event hashes the same from one discovery to the next.

Events are recorded as Kalshi sent them rather than as karb models them, because karb's model
changes. The first live run showed classification needed a field (``custom_strike``) that the
first model ignored (ADR-0006); a recording that kept only the model could never have been
replayed through the fix (docs/decisions/ADR-0007).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final

from karb.arb.detect import DetectConfig
from karb.arb.opportunity import Opportunity
from karb.core.fixed import Cash, Price, Qty
from karb.market.book import Level, OrderBook
from karb.market.fees import FeeConfig, RoundingMode

__all__ = [
    "VOLATILE_EVENT_FIELDS",
    "VOLATILE_MARKET_FIELDS",
    "book_from_row",
    "book_to_row",
    "detect_config_from_json",
    "detect_config_to_json",
    "dumps_exact",
    "from_ns",
    "legs_json",
    "payload_hash",
    "seconds_to_ns",
    "structural_payload",
    "to_ns",
]

VOLATILE_MARKET_FIELDS: Final = frozenset(
    {
        "yes_bid_dollars",
        "yes_ask_dollars",
        "no_bid_dollars",
        "no_ask_dollars",
        "yes_bid_size_fp",
        "yes_ask_size_fp",
        "last_price_dollars",
        "previous_price_dollars",
        "previous_yes_bid_dollars",
        "previous_yes_ask_dollars",
        "volume_fp",
        "volume_24h_fp",
        "open_interest_fp",
        "liquidity_dollars",
        "updated_time",
    }
)
"""Market fields that move with every trade or quote. Order books are recorded separately."""

VOLATILE_EVENT_FIELDS: Final = frozenset({"last_updated_ts"})

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value.is_finite():
            as_float = float(value)
            # repr() is the shortest round-tripping string, so every decimal Kalshi realistically
            # sends survives as a JSON number. Anything a float cannot hold exactly stays text,
            # which the wire models parse back to the identical Decimal.
            if Decimal(repr(as_float)) == value:
                return as_float
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def dumps_exact(value: Any) -> str:
    """Compact, key-sorted JSON that decodes (with ``load_json``) to the same numbers."""
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def structural_payload(raw_event: Mapping[str, Any]) -> dict[str, Any]:
    """An event as recorded: everything but quote-like fields, markets sorted by ticker."""
    event = {
        key: value
        for key, value in raw_event.items()
        if key not in VOLATILE_EVENT_FIELDS and key != "markets"
    }
    markets = [
        {key: value for key, value in market.items() if key not in VOLATILE_MARKET_FIELDS}
        for market in raw_event.get("markets") or []
        if isinstance(market, Mapping)
    ]
    event["markets"] = sorted(markets, key=lambda market: str(market.get("ticker", "")))
    return event


def payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(dumps_exact(payload).encode()).hexdigest()


def to_ns(moment: datetime) -> int:
    """Nanoseconds since the Unix epoch, exactly (datetimes carry microseconds)."""
    if moment.tzinfo is None:
        raise ValueError("recordings take timezone-aware datetimes only")
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def from_ns(ns: int) -> datetime:
    return _EPOCH + timedelta(microseconds=ns // 1_000)


def seconds_to_ns(seconds: float) -> int:
    return round(seconds * 1_000_000_000)


BookRow = tuple[list[int], list[int], list[int], list[int]]


def book_to_row(book: OrderBook) -> BookRow:
    """Bid ladders as parallel integer lists, best level first."""
    return (
        [level.price.raw for level in book.yes_bids],
        [level.qty.raw for level in book.yes_bids],
        [level.price.raw for level in book.no_bids],
        [level.qty.raw for level in book.no_bids],
    )


def book_from_row(
    ticker: str,
    yes_prices: Sequence[int],
    yes_qtys: Sequence[int],
    no_prices: Sequence[int],
    no_qtys: Sequence[int],
) -> OrderBook:
    return OrderBook(
        ticker,
        tuple(Level(Price(p), Qty(q)) for p, q in zip(yes_prices, yes_qtys, strict=True)),
        tuple(Level(Price(p), Qty(q)) for p, q in zip(no_prices, no_qtys, strict=True)),
    )


def detect_config_to_json(config: DetectConfig) -> dict[str, Any]:
    fees = config.fees
    return {
        "taker_coefficient": f"{fees.taker_coefficient.numerator}/{fees.taker_coefficient.denominator}",
        "balance_unit": fees.balance_unit,
        "rounding_mode": fees.rounding_mode.value,
        "min_profit": config.min_profit.raw,
        "max_levels": config.max_levels,
        "max_contracts_per_leg": config.max_contracts_per_leg,
        "max_cost": None if config.max_cost is None else config.max_cost.raw,
        "min_apr": config.min_apr,
    }


def detect_config_from_json(data: Mapping[str, Any]) -> DetectConfig:
    max_contracts = data["max_contracts_per_leg"]
    max_cost = data["max_cost"]
    return DetectConfig(
        fees=FeeConfig(
            taker_coefficient=Fraction(str(data["taker_coefficient"])),
            balance_unit=int(data["balance_unit"]),
            rounding_mode=RoundingMode(data["rounding_mode"]),
        ),
        min_profit=Cash(int(data["min_profit"])),
        max_levels=int(data["max_levels"]),
        max_contracts_per_leg=None if max_contracts is None else int(max_contracts),
        max_cost=None if max_cost is None else Cash(int(max_cost)),
        # Absent from recordings made before the hurdle existed.
        min_apr=None if data.get("min_apr") is None else float(data["min_apr"]),
    )


def legs_json(opportunity: Opportunity) -> str:
    """Every fill of every leg, in raw exact units."""
    return dumps_exact(
        [
            {
                "ticker": leg.ticker,
                "side": leg.side.value,
                "qty": leg.order.qty.raw,
                "cash_out": leg.order.cash_out.raw,
                "fills": [
                    {
                        "price": fill.price.raw,
                        "qty": fill.qty.raw,
                        "trade_fee": fill.trade_fee.raw,
                        "rounding_fee": fill.rounding_fee.raw,
                        "rebate": fill.rebate.raw,
                    }
                    for fill in leg.order.fills
                ],
            }
            for leg in opportunity.basket.legs
        ]
    )
