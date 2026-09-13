"""Kalshi-native fixed-point units.

Kalshi quotes prices as dollar strings with up to four decimal places and contract counts
with two (docs/decisions/ADR-0001). Every money quantity in the scanner is an exact integer
at one of three scales:

``Price``  integer units of $0.0001, always within [$0, $1]
``Qty``    integer units of 0.01 contracts, never negative
``Cash``   integer units of $0.000001, signed

The scales are chosen so that ``Price.raw * Qty.raw`` is *exactly* a ``Cash.raw``: a notional
never rounds, and fee math -- which Kalshi itself performs at six decimal places -- lands on
the same grid.

Floats appear only where a value leaves the money path: the LP that *proposes* baskets (it
never prices them) and display. Rounding happens only through ``div_round`` and always names
its mode; anything that rounds a cost rounds it against the trader.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Final

__all__ = [
    "CASH_DECIMALS",
    "CASH_SCALE",
    "PRICE_DECIMALS",
    "PRICE_SCALE",
    "QTY_DECIMALS",
    "QTY_SCALE",
    "Cash",
    "FixedPointError",
    "Price",
    "Qty",
    "Rounding",
    "div_round",
    "format_scaled",
    "parse_scaled",
]

PRICE_DECIMALS: Final = 4
QTY_DECIMALS: Final = 2
CASH_DECIMALS: Final = 6
PRICE_SCALE: Final = 10**PRICE_DECIMALS
QTY_SCALE: Final = 10**QTY_DECIMALS
CASH_SCALE: Final = 10**CASH_DECIMALS

_PAYOUT_PER_QTY_RAW: Final = CASH_SCALE // QTY_SCALE
"""Cash raw units paid by one raw ``Qty`` unit (0.01 contract) of a winning contract."""

_PLAIN_DECIMAL: Final = re.compile(r"(-?)(\d+)(?:\.(\d*))?")


class FixedPointError(ValueError):
    """A value is out of range, or not exactly representable at its unit's scale."""


class Rounding(Enum):
    """How an inexact division resolves. ``FLOOR`` and ``CEIL`` are toward -inf and +inf."""

    FLOOR = "floor"
    CEIL = "ceil"
    HALF_EVEN = "half_even"


def div_round(numerator: int, denominator: int, rounding: Rounding) -> int:
    """Divide integers exactly, rounding the quotient as named."""
    if denominator <= 0:
        raise ValueError(f"denominator must be positive, got {denominator}")
    quotient, remainder = divmod(numerator, denominator)
    if remainder == 0 or rounding is Rounding.FLOOR:
        return quotient
    if rounding is Rounding.CEIL:
        return quotient + 1
    twice = 2 * remainder
    if twice < denominator:
        return quotient
    if twice > denominator:
        return quotient + 1
    return quotient + (quotient & 1)


def parse_scaled(text: str, decimals: int, *, signed: bool = False) -> int:
    """Parse a plain decimal string into an integer count of ``10**-decimals``, exactly.

    Digits beyond the scale are accepted only when they are zeros: ``"0.180000"`` is the
    price ``0.1800``, while ``"0.18005"`` cannot be represented and is rejected rather than
    silently rounded.
    """
    match = _PLAIN_DECIMAL.fullmatch(text)
    if match is None:
        raise FixedPointError(f"not a plain decimal string: {text!r}")
    sign, whole, fraction = match.group(1), match.group(2), match.group(3) or ""
    if sign and not signed:
        raise FixedPointError(f"negative value where none is allowed: {text!r}")
    if len(fraction) > decimals:
        if fraction[decimals:].strip("0"):
            raise FixedPointError(f"{text!r} is not exact at {decimals} decimal places")
        fraction = fraction[:decimals]
    raw: int = int(whole) * 10**decimals + int(fraction.ljust(decimals, "0") or "0")
    return -raw if sign else raw


def format_scaled(raw: int, decimals: int) -> str:
    """Render an integer count of ``10**-decimals`` as a plain decimal string."""
    whole, fraction = divmod(abs(raw), 10**decimals)
    sign = "-" if raw < 0 else ""
    if decimals == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:0{decimals}d}"


def _check_raw(owner: str, raw: object) -> None:
    # `type(...) is int` rejects bool and numpy integers: both pass isinstance, and both
    # have quietly corrupted fixed-point code before.
    if type(raw) is not int:
        raise TypeError(f"{owner}.raw must be an int, got {type(raw).__name__}")


def _require_same_unit(left: object, right: object) -> None:
    if type(left) is not type(right):
        raise TypeError(f"cannot combine {type(left).__name__} with {type(right).__name__}")


@dataclass(frozen=True, slots=True, order=True)
class Price:
    """A contract price in integer units of $0.0001, within [$0, $1]."""

    raw: int

    ZERO: ClassVar[Price]
    ONE: ClassVar[Price]

    def __post_init__(self) -> None:
        _check_raw("Price", self.raw)
        if not 0 <= self.raw <= PRICE_SCALE:
            raise FixedPointError(
                f"price outside [0, 1]: {format_scaled(self.raw, PRICE_DECIMALS)}"
            )

    @classmethod
    def parse(cls, text: str) -> Price:
        return cls(parse_scaled(text, PRICE_DECIMALS))

    def complement(self) -> Price:
        """The other side's price: a bid for YES at p is an ask for NO at 1 - p."""
        return Price(PRICE_SCALE - self.raw)

    def notional(self, qty: Qty) -> Cash:
        """Exact cost of ``qty`` contracts at this price. Never rounds."""
        _require_same_unit(Qty.ZERO, qty)
        return Cash(self.raw * qty.raw)

    def to_float(self) -> float:
        return self.raw / PRICE_SCALE

    def __str__(self) -> str:
        return format_scaled(self.raw, PRICE_DECIMALS)


@dataclass(frozen=True, slots=True, order=True)
class Qty:
    """A contract count in integer units of 0.01 contracts. Never negative."""

    raw: int

    ZERO: ClassVar[Qty]

    def __post_init__(self) -> None:
        _check_raw("Qty", self.raw)
        if self.raw < 0:
            raise FixedPointError(f"negative quantity: {format_scaled(self.raw, QTY_DECIMALS)}")

    @classmethod
    def parse(cls, text: str) -> Qty:
        return cls(parse_scaled(text, QTY_DECIMALS))

    @classmethod
    def contracts(cls, whole: int) -> Qty:
        return cls(whole * QTY_SCALE)

    def __add__(self, other: Qty) -> Qty:
        _require_same_unit(self, other)
        return Qty(self.raw + other.raw)

    def __sub__(self, other: Qty) -> Qty:
        _require_same_unit(self, other)
        return Qty(self.raw - other.raw)

    @property
    def is_zero(self) -> bool:
        return self.raw == 0

    def floor_whole(self) -> Qty:
        """Drop any fractional contract."""
        return Qty(self.raw - self.raw % QTY_SCALE)

    def payout(self) -> Cash:
        """What these contracts pay if they win: $1 each."""
        return Cash(self.raw * _PAYOUT_PER_QTY_RAW)

    def to_float(self) -> float:
        return self.raw / QTY_SCALE

    def __str__(self) -> str:
        return format_scaled(self.raw, QTY_DECIMALS)


@dataclass(frozen=True, slots=True, order=True)
class Cash:
    """A signed dollar amount in integer units of $0.000001."""

    raw: int

    ZERO: ClassVar[Cash]

    def __post_init__(self) -> None:
        _check_raw("Cash", self.raw)

    @classmethod
    def parse(cls, text: str) -> Cash:
        return cls(parse_scaled(text, CASH_DECIMALS, signed=True))

    @classmethod
    def total(cls, amounts: Iterable[Cash]) -> Cash:
        raw = 0
        for amount in amounts:
            _require_same_unit(Cash.ZERO, amount)
            raw += amount.raw
        return cls(raw)

    def __add__(self, other: Cash) -> Cash:
        _require_same_unit(self, other)
        return Cash(self.raw + other.raw)

    def __sub__(self, other: Cash) -> Cash:
        _require_same_unit(self, other)
        return Cash(self.raw - other.raw)

    def __neg__(self) -> Cash:
        return Cash(-self.raw)

    def round_to(self, unit: int, rounding: Rounding) -> Cash:
        """Snap to a multiple of ``unit`` raw units (e.g. 10_000 for whole cents)."""
        return Cash(div_round(self.raw, unit, rounding) * unit)

    def to_float(self) -> float:
        return self.raw / CASH_SCALE

    def dollars(self, decimals: int = 2) -> str:
        """Display form, e.g. ``$1.23``. Half-even: display has no counterparty to protect."""
        scaled = div_round(abs(self.raw), 10 ** (CASH_DECIMALS - decimals), Rounding.HALF_EVEN)
        sign = "-" if self.raw < 0 and scaled else ""
        return f"{sign}${format_scaled(scaled, decimals)}"

    def __str__(self) -> str:
        return format_scaled(self.raw, CASH_DECIMALS)


Price.ZERO = Price(0)
Price.ONE = Price(PRICE_SCALE)
Qty.ZERO = Qty(0)
Cash.ZERO = Cash(0)
