"""Orders: what karb sends to the exchange, and what came back (docs/decisions/ADR-0010).

Every order is an immediate-or-cancel limit **buy**. Kalshi's order API speaks only of the YES
leg: ``bid`` buys YES and ``ask`` sells YES, which is the same exposure as buying NO. Prices are
always YES-leg prices, so buying NO at no more than ``q`` is sent as an ``ask`` at ``1 - q``.

Fills come back exactly -- price, count and fee per fill -- and become a ``LegFill`` priced with
the exchange's own trade fees, plus the documented rounding fee that aligns each fill to the
balance precision. The same fills are also priced with karb's fee model, so every trade checks
the model against what the exchange actually charged; the account balance then checks both.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from karb.arb.opportunity import LegFill, VerifiedBasket
from karb.core.fixed import (
    PRICE_DECIMALS,
    QTY_DECIMALS,
    Cash,
    FixedPointError,
    Price,
    Qty,
    Rounding,
    div_round,
    format_scaled,
)
from karb.market.book import Level, OrderBook, Side
from karb.market.fees import (
    CENT_BALANCE_UNIT,
    FeeConfig,
    FeeSchedule,
    FillCost,
    OrderCost,
    taker_buy_cost,
)

__all__ = [
    "SELF_TRADE_PREVENTION",
    "TIME_IN_FORCE",
    "ExchangeFill",
    "Order",
    "PlacedOrder",
    "client_order_id",
    "order_request",
    "parse_fill",
    "parse_wire_price",
    "plan_orders",
    "position_cost",
    "position_legs",
    "walk",
]

TIME_IN_FORCE: Final = "immediate_or_cancel"
SELF_TRADE_PREVENTION: Final = "taker_at_cross"
_CLIENT_ID_NAMESPACE: Final = uuid.UUID("6f2b8f5e-1d0c-4c8e-9a43-2b5d7f1e9c30")


@dataclass(frozen=True, slots=True)
class Order:
    """A taker buy of ``qty`` contracts of ``side``, at prices no worse than ``limit``."""

    ticker: str
    side: Side
    limit: Price
    qty: Qty


def plan_orders(basket: VerifiedBasket) -> tuple[Order, ...]:
    """One order per leg, limited to the worst price the decision book needed."""
    return tuple(
        Order(leg.ticker, leg.side, max(fill.price for fill in leg.order.fills), leg.order.qty)
        for leg in basket.legs
    )


def walk(book: OrderBook, side: Side, qty: Qty, limit: Price | None) -> tuple[Level, ...]:
    """The levels a buy of ``qty`` at or below ``limit`` would take, cheapest first."""
    fills: list[Level] = []
    remaining = qty.raw
    for level in book.asks(side):
        if remaining == 0 or (limit is not None and level.price > limit):
            break
        take = min(remaining, level.qty.raw)
        fills.append(Level(level.price, Qty(take)))
        remaining -= take
    return tuple(fills)


def position_legs(*phases: Sequence[PlacedOrder]) -> tuple[LegFill, ...]:
    legs: list[LegFill] = []
    for phase in phases:
        for placed in phase:
            leg = placed.leg_fill()
            if leg is not None:
                legs.append(leg)
    return tuple(legs)


def position_cost(legs: Iterable[LegFill]) -> Cash:
    return Cash.total(leg.order.cash_out for leg in legs)


def client_order_id(trade_id: str, phase: str, seq: int) -> str:
    """Deterministic per order, so a retried request is recognised as the same order."""
    return str(uuid.uuid5(_CLIENT_ID_NAMESPACE, f"{trade_id}/{phase}/{seq}"))


def order_request(order: Order, client_id: str) -> dict[str, str]:
    """The V2 create-order body for ``order``."""
    yes_price = order.limit if order.side is Side.YES else order.limit.complement()
    return {
        "ticker": order.ticker,
        "side": "bid" if order.side is Side.YES else "ask",
        "count": format_scaled(order.qty.raw, QTY_DECIMALS),
        "price": format_scaled(yes_price.raw, PRICE_DECIMALS),
        "time_in_force": TIME_IN_FORCE,
        "self_trade_prevention_type": SELF_TRADE_PREVENTION,
        "client_order_id": client_id,
    }


def parse_wire_price(text: str) -> Price:
    """A dollar price from the exchange. Responses may pad to six decimals; anything finer than
    the $0.0001 grid is an error, never rounded away."""
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise FixedPointError(f"not a price: {text!r}") from exc
    scaled = value.scaleb(PRICE_DECIMALS)
    if scaled != scaled.to_integral_value():
        raise FixedPointError(f"price finer than $0.0001: {text!r}")
    return Price(int(scaled))


@dataclass(frozen=True, slots=True)
class ExchangeFill:
    fill_id: str
    order_id: str
    ticker: str
    side: Side
    price: Price
    """What one contract of ``side`` cost."""
    qty: Qty
    fee: Cash
    is_taker: bool


def parse_fill(raw: Mapping[str, Any]) -> ExchangeFill:
    side = Side(str(raw["outcome_side"]))
    price_field = "yes_price_dollars" if side is Side.YES else "no_price_dollars"
    return ExchangeFill(
        fill_id=str(raw.get("fill_id") or raw.get("trade_id") or ""),
        order_id=str(raw["order_id"]),
        ticker=str(raw.get("ticker") or raw.get("market_ticker") or ""),
        side=side,
        price=parse_wire_price(str(raw[price_field])),
        qty=Qty.parse(str(raw["count_fp"])),
        fee=Cash.parse(str(raw["fee_cost"])),
        is_taker=bool(raw.get("is_taker", True)),
    )


@dataclass(frozen=True, slots=True)
class PlacedOrder:
    """One order as the exchange handled it."""

    order: Order
    client_order_id: str
    order_id: str | None
    """``None`` when the exchange never accepted the order."""
    fills: tuple[ExchangeFill, ...] = ()
    error: str = ""
    response: Mapping[str, Any] | None = None
    balance_unit: int = CENT_BALANCE_UNIT
    """The account's balance precision, in raw cash: each fill is aligned to it."""

    @property
    def filled(self) -> Qty:
        total = Qty.ZERO
        for fill in self.fills:
            total = total + fill.qty
        return total

    def leg_fill(self) -> LegFill | None:
        """What traded, priced with the exchange's trade fees and the documented per-fill
        rounding to the balance precision (no rebates assumed); ``None`` when nothing did."""
        if not self.fills:
            return None
        unit = self.balance_unit
        costs: list[FillCost] = []
        for fill in self.fills:
            notional = fill.price.notional(fill.qty)
            before_alignment = -(notional.raw + fill.fee.raw)
            aligned = div_round(before_alignment, unit, Rounding.FLOOR) * unit
            costs.append(
                FillCost(
                    price=fill.price,
                    qty=fill.qty,
                    notional=notional,
                    trade_fee=fill.fee,
                    rounding_fee=Cash(before_alignment - aligned),
                    rebate=Cash.ZERO,
                )
            )
        return LegFill(self.order.ticker, self.order.side, OrderCost(tuple(costs)))

    @property
    def cash_out(self) -> Cash:
        leg = self.leg_fill()
        return Cash.ZERO if leg is None else leg.order.cash_out

    @property
    def fees(self) -> Cash:
        """Trade fees as the exchange reported them, plus per-fill balance rounding."""
        leg = self.leg_fill()
        return Cash.ZERO if leg is None else leg.order.fees

    @property
    def exchange_fees(self) -> Cash:
        """Trade fees exactly as the exchange reported them."""
        return Cash.total(fill.fee for fill in self.fills)

    def model_fees(self, schedule: FeeSchedule, config: FeeConfig) -> Cash:
        """karb's fee model applied to exactly these fills, one exchange fill at a time."""
        if not self.fills:
            return Cash.ZERO
        levels = [Level(fill.price, fill.qty) for fill in self.fills]
        return taker_buy_cost(levels, schedule, config).fees

    def response_json(self) -> str:
        return "" if self.response is None else json.dumps(self.response, default=str)
