"""Kalshi trading fees, rounded the way the exchange rounds them.

Fee model (docs/decisions/ADR-0002). A taker fill of C contracts at price P costs a trade fee
of ``rate * C * P * (1 - P)`` dollars, where ``rate`` is the 0.07 taker coefficient times the
series (or event override) multiplier. Kalshi's documented rounding then applies per fill:

1. ``trade_fee = ceil_6dp(model_fee)``
2. ``aligned = floor_to_balance_unit(revenue - trade_fee)``  (revenue is -notional for a buy)
3. ``rounding_fee = (revenue - trade_fee) - aligned``
4. a per-order accumulator may later rebate rounding overpayment in whole balance units,
   capped so no fill's net fee goes negative.

``RoundingMode.WORST_CASE`` (the default) never assumes a rebate: every fill is aligned
against the trader in isolation, which is an upper bound on what the exchange charges.
``RoundingMode.ACCUMULATOR`` simulates the documented accumulator.

The 0.07 coefficient is corroborated by Kalshi's own fee-rounding example: one contract
bought at $0.055 has a model fee of $0.00363825 = 0.07 x 0.055 x 0.945.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from karb.core.fixed import PRICE_SCALE, Cash, Price, Qty, Rounding, div_round
from karb.market.book import Level
from karb.market.model import EventInfo, SeriesInfo

__all__ = [
    "CENTICENT_BALANCE_UNIT",
    "CENT_BALANCE_UNIT",
    "DEFAULT_TAKER_COEFFICIENT",
    "FeeConfig",
    "FeeResolution",
    "FeeSchedule",
    "FeeType",
    "FillCost",
    "OrderCost",
    "RoundingMode",
    "resolve_fee_schedule",
    "taker_buy_cost",
    "taker_trade_fee",
]

DEFAULT_TAKER_COEFFICIENT: Final = Fraction(7, 100)
CENT_BALANCE_UNIT: Final = 10_000
"""$0.01 in ``Cash`` raw units: the balance precision of non-direct members."""
CENTICENT_BALANCE_UNIT: Final = 100
"""$0.0001 in ``Cash`` raw units: the balance precision of direct members."""


class FeeType(StrEnum):
    QUADRATIC = "quadratic"
    QUADRATIC_WITH_MAKER_FEES = "quadratic_with_maker_fees"
    QUADRATIC_WITH_COMBO_MAKER_FEES = "quadratic_with_combo_maker_fees"
    FLAT = "flat"


# Maker variants differ only in what resting orders pay; the taker side is the same curve.
_TAKER_QUADRATIC: Final = frozenset(
    {
        FeeType.QUADRATIC,
        FeeType.QUADRATIC_WITH_MAKER_FEES,
        FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES,
    }
)


class RoundingMode(StrEnum):
    WORST_CASE = "worst_case"
    ACCUMULATOR = "accumulator"


@dataclass(frozen=True, slots=True)
class FeeConfig:
    taker_coefficient: Fraction = DEFAULT_TAKER_COEFFICIENT
    balance_unit: int = CENT_BALANCE_UNIT
    rounding_mode: RoundingMode = RoundingMode.WORST_CASE

    def __post_init__(self) -> None:
        if self.taker_coefficient < 0:
            raise ValueError("taker coefficient cannot be negative")
        if self.balance_unit <= 0:
            raise ValueError("balance unit must be positive")


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    fee_type: FeeType
    multiplier: Fraction
    source: str
    """Where the schedule came from: ``"series"`` or ``"event override"``."""

    def taker_rate(self, config: FeeConfig) -> Fraction:
        """Dollars of fee per contract, per unit of ``P * (1 - P)``."""
        return config.taker_coefficient * self.multiplier


@dataclass(frozen=True, slots=True)
class FeeResolution:
    schedule: FeeSchedule | None
    reason: str = ""
    """Why no schedule could be resolved. Empty when ``schedule`` is set."""


def resolve_fee_schedule(event: EventInfo, series: SeriesInfo | None) -> FeeResolution:
    """The event's override when present, otherwise its series' schedule. Fails closed."""
    override_type, override_multiplier = event.fee_type_override, event.fee_multiplier_override
    if override_type is not None or override_multiplier is not None:
        if override_type is None or override_multiplier is None:
            return FeeResolution(None, "event fee override is only half specified")
        return _schedule(override_type, override_multiplier, "event override")
    if series is None:
        return FeeResolution(None, f"series {event.series_ticker!r} is unknown")
    if series.fee_multiplier is None:
        return FeeResolution(None, f"series {series.ticker!r} has no fee multiplier")
    return _schedule(series.fee_type, series.fee_multiplier, "series")


def _schedule(fee_type: str, multiplier: Decimal, source: str) -> FeeResolution:
    try:
        kind = FeeType(fee_type)
    except ValueError:
        return FeeResolution(None, f"unknown fee type {fee_type!r}")
    if kind not in _TAKER_QUADRATIC:
        return FeeResolution(None, f"fee type {fee_type!r} is not modelled")
    if not multiplier.is_finite() or multiplier < 0:
        return FeeResolution(None, f"invalid fee multiplier {multiplier}")
    return FeeResolution(FeeSchedule(kind, Fraction(multiplier), source))


def taker_trade_fee(price: Price, qty: Qty, rate: Fraction) -> Cash:
    """``rate * C * P * (1 - P)`` dollars, rounded up to the next $0.000001.

    With C = q / 100 contracts and P = p / 10^4, the fee in $0.000001 units is
    ``rate * q * p * (10^4 - p) / 10^4``.
    """
    exact = rate * qty.raw * price.raw * (PRICE_SCALE - price.raw) / PRICE_SCALE
    return Cash(math.ceil(exact))


@dataclass(frozen=True, slots=True)
class FillCost:
    price: Price
    qty: Qty
    notional: Cash
    trade_fee: Cash
    rounding_fee: Cash
    rebate: Cash

    @property
    def fee(self) -> Cash:
        return self.trade_fee + self.rounding_fee - self.rebate

    @property
    def cash_out(self) -> Cash:
        return self.notional + self.fee


@dataclass(frozen=True, slots=True)
class OrderCost:
    """One taker order that sweeps one or more price levels."""

    fills: tuple[FillCost, ...]

    @property
    def qty(self) -> Qty:
        total = Qty.ZERO
        for fill in self.fills:
            total = total + fill.qty
        return total

    @property
    def notional(self) -> Cash:
        return Cash.total(fill.notional for fill in self.fills)

    @property
    def fees(self) -> Cash:
        return Cash.total(fill.fee for fill in self.fills)

    @property
    def cash_out(self) -> Cash:
        return Cash.total(fill.cash_out for fill in self.fills)


def taker_buy_cost(fills: Sequence[Level], schedule: FeeSchedule, config: FeeConfig) -> OrderCost:
    """Exact cash out of one buy order filled at ``fills``, in the order given."""
    rate = schedule.taker_rate(config)
    unit = config.balance_unit
    accumulator = 0
    costs: list[FillCost] = []
    for level in fills:
        notional = level.price.notional(level.qty)
        trade_fee = taker_trade_fee(level.price, level.qty, rate)
        before_alignment = -(notional.raw + trade_fee.raw)
        aligned = div_round(before_alignment, unit, Rounding.FLOOR) * unit
        rounding_fee = before_alignment - aligned
        rebate = 0
        if config.rounding_mode is RoundingMode.ACCUMULATOR:
            accumulator += rounding_fee
            rebate = min(accumulator, trade_fee.raw + rounding_fee) // unit * unit
            accumulator -= rebate
        costs.append(
            FillCost(
                price=level.price,
                qty=level.qty,
                notional=notional,
                trade_fee=trade_fee,
                rounding_fee=Cash(rounding_fee),
                rebate=Cash(rebate),
            )
        )
    return OrderCost(tuple(costs))
