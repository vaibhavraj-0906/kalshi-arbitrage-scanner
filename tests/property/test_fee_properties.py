from __future__ import annotations

from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from karb.core.fixed import PRICE_SCALE, Cash, Price, Qty
from karb.market.book import Level
from karb.market.fees import FeeConfig, FeeSchedule, FeeType, RoundingMode, taker_buy_cost

levels = st.builds(
    Level,
    st.integers(min_value=1, max_value=PRICE_SCALE - 1).map(Price),
    st.integers(min_value=1, max_value=10**7).map(Qty),
)
schedules = st.builds(
    FeeSchedule,
    st.just(FeeType.QUADRATIC),
    st.sampled_from([Fraction(0), Fraction(1, 2), Fraction(1), Fraction(2)]),
    st.just("series"),
)
units = st.sampled_from([100, 10_000])


def exact_model_fee(level: Level, schedule: FeeSchedule) -> Fraction:
    p = Fraction(level.price.raw, PRICE_SCALE)
    return Fraction(7, 100) * schedule.multiplier * Fraction(level.qty.raw, 100) * p * (1 - p)


@given(st.lists(levels, min_size=1, max_size=6), schedules, units)
def test_worst_case_never_undercharges_and_rounds_by_less_than_a_unit(
    fills: list[Level], schedule: FeeSchedule, unit: int
) -> None:
    config = FeeConfig(balance_unit=unit)
    for fill in taker_buy_cost(fills, schedule, config).fills:
        exact_fee_micro = exact_model_fee(Level(fill.price, fill.qty), schedule) * 10**6
        assert fill.trade_fee.raw >= exact_fee_micro > fill.trade_fee.raw - 1
        assert fill.cash_out.raw % unit == 0
        assert fill.cash_out.raw >= fill.notional.raw + exact_fee_micro
        assert fill.rounding_fee.raw < unit
        assert fill.rebate == Cash.ZERO


@given(st.lists(levels, min_size=1, max_size=8), schedules, units)
def test_accumulator_is_never_worse_than_worst_case_and_never_pays_the_trader(
    fills: list[Level], schedule: FeeSchedule, unit: int
) -> None:
    worst = taker_buy_cost(fills, schedule, FeeConfig(balance_unit=unit))
    accumulated = taker_buy_cost(
        fills, schedule, FeeConfig(balance_unit=unit, rounding_mode=RoundingMode.ACCUMULATOR)
    )
    assert accumulated.cash_out <= worst.cash_out
    assert (worst.cash_out - accumulated.cash_out).raw % unit == 0
    for fill in accumulated.fills:
        assert fill.fee.raw >= 0
        assert (fill.notional.raw + fill.fee.raw) % unit == 0
