from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from karb.core.fixed import (
    PRICE_SCALE,
    Cash,
    FixedPointError,
    Price,
    Qty,
    Rounding,
    div_round,
    format_scaled,
)

prices = st.integers(min_value=0, max_value=PRICE_SCALE).map(Price)
quantities = st.integers(min_value=0, max_value=10**9).map(Qty)


@given(prices, st.integers(min_value=0, max_value=4))
def test_price_round_trips_through_text(price: Price, extra_zeros: int) -> None:
    assert Price.parse(str(price) + "0" * extra_zeros) == price


@given(prices, st.integers(min_value=1, max_value=9))
def test_price_with_a_fifth_significant_decimal_is_rejected(price: Price, digit: int) -> None:
    if price == Price.ONE:
        return
    with pytest.raises(FixedPointError):
        Price.parse(f"{price}{digit}")


@given(quantities)
def test_qty_round_trips_through_text(qty: Qty) -> None:
    assert Qty.parse(str(qty)) == qty


@given(st.integers(min_value=-(10**15), max_value=10**15))
def test_cash_round_trips_through_text(raw: int) -> None:
    assert Cash.parse(format_scaled(raw, 6)) == Cash(raw)


@given(prices, quantities)
def test_notional_is_exact(price: Price, qty: Qty) -> None:
    exact = Fraction(price.raw, PRICE_SCALE) * Fraction(qty.raw, 100)
    assert Fraction(price.notional(qty).raw, 10**6) == exact


@given(
    st.integers(min_value=-(10**12), max_value=10**12), st.integers(min_value=1, max_value=10**6)
)
def test_div_round_brackets_the_exact_quotient(numerator: int, denominator: int) -> None:
    exact = Fraction(numerator, denominator)
    floor = div_round(numerator, denominator, Rounding.FLOOR)
    ceil = div_round(numerator, denominator, Rounding.CEIL)
    half = div_round(numerator, denominator, Rounding.HALF_EVEN)
    assert floor <= exact <= ceil
    assert ceil - floor == (0 if exact.denominator == 1 else 1)
    assert abs(half - exact) <= Fraction(1, 2)


@given(st.integers(min_value=-(10**12), max_value=10**12), st.sampled_from([100, 10_000]))
def test_balance_alignment_is_on_grid_and_adverse(raw: int, unit: int) -> None:
    down = Cash(raw).round_to(unit, Rounding.FLOOR)
    up = Cash(raw).round_to(unit, Rounding.CEIL)
    assert down.raw % unit == 0 and up.raw % unit == 0
    assert down.raw <= raw <= up.raw
    assert up.raw - down.raw in (0, unit)
