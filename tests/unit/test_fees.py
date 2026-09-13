from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from karb.core.fixed import Cash, Price, Qty
from karb.market.fees import (
    FeeConfig,
    FeeSchedule,
    FeeType,
    RoundingMode,
    resolve_fee_schedule,
    taker_buy_cost,
    taker_trade_fee,
)
from karb.market.model import SeriesInfo
from tests.support import fixture_event, fixture_series, lv

QUADRATIC = FeeSchedule(FeeType.QUADRATIC, Fraction(1), "series")
RATE = Fraction(7, 100)


def test_fee_at_fifty_cents() -> None:
    # 0.07 x 1 x 0.50 x 0.50 = $0.0175
    assert taker_trade_fee(Price.parse("0.50"), Qty.contracts(1), RATE) == Cash(17_500)


def test_kalshi_fee_rounding_worked_example() -> None:
    """docs.kalshi.com/getting_started/fee_rounding, "FCM-cleared fill".

    A buy with -$0.055 of revenue and a model fee of $0.00363825 -- which is exactly
    0.07 x 1 contract x 0.055 x 0.945, corroborating the 0.07 taker coefficient.
    """
    fill = taker_buy_cost([lv("0.0550", "1.00")], QUADRATIC, FeeConfig()).fills[0]
    assert fill.notional == Cash(55_000)
    assert fill.trade_fee == Cash(3_639)  # ceil_6dp($0.00363825)
    assert fill.rounding_fee == Cash(1_361)
    assert fill.cash_out == Cash(60_000)  # balance changes by exactly -$0.06
    assert fill.fee == Cash(5_000)


def test_accumulator_rebates_only_whole_units_and_never_below_zero() -> None:
    small = lv("0.0600", "0.10")  # notional $0.006, trade fee $0.000395 -> rounding $0.003605
    large = lv("0.5000", "100.00")  # lands exactly on the cent grid: no rounding of its own
    fills = [small, small, small, large]
    worst = taker_buy_cost(fills, QUADRATIC, FeeConfig())
    accumulated = taker_buy_cost(
        fills, QUADRATIC, FeeConfig(rounding_mode=RoundingMode.ACCUMULATOR)
    )

    assert [f.rounding_fee.raw for f in worst.fills] == [3_605, 3_605, 3_605, 0]
    # Each small fill's cap (trade fee + rounding = $0.004) is below one cent, so nothing is
    # rebated until the large fill, whose fee can absorb a $0.01 rebate of the $0.010815 owed.
    assert [f.rebate.raw for f in accumulated.fills] == [0, 0, 0, 10_000]
    assert worst.cash_out - accumulated.cash_out == Cash(10_000)
    assert all(f.fee.raw >= 0 for f in accumulated.fills)


def test_direct_member_precision_rounds_less() -> None:
    cents = taker_buy_cost([lv("0.0550", "1.00")], QUADRATIC, FeeConfig())
    centicents = taker_buy_cost([lv("0.0550", "1.00")], QUADRATIC, FeeConfig(balance_unit=100))
    assert centicents.cash_out == Cash(58_700)
    assert centicents.cash_out < cents.cash_out


def test_zero_multiplier_is_free_but_still_aligned() -> None:
    free = FeeSchedule(FeeType.QUADRATIC, Fraction(0), "series")
    cost = taker_buy_cost([lv("0.0550", "1.00")], free, FeeConfig())
    assert cost.fills[0].trade_fee == Cash(0)
    assert cost.cash_out == Cash(60_000)


class TestResolution:
    def test_series_schedule(self) -> None:
        event = fixture_event("events_KXINX.json")
        resolution = resolve_fee_schedule(event, fixture_series()["KXINX"])
        assert resolution.schedule == FeeSchedule(FeeType.QUADRATIC, Fraction(1), "series")

    def test_event_override_wins(self) -> None:
        event = replace(
            fixture_event("events_KXINX.json"),
            fee_type_override="quadratic_with_maker_fees",
            fee_multiplier_override=Decimal("0.5"),
        )
        schedule = resolve_fee_schedule(event, fixture_series()["KXINX"]).schedule
        assert schedule == FeeSchedule(
            FeeType.QUADRATIC_WITH_MAKER_FEES, Fraction(1, 2), "event override"
        )

    @pytest.mark.parametrize(
        ("override_type", "override_multiplier", "series_type", "reason"),
        [
            ("quadratic", None, "quadratic", "half specified"),
            (None, None, "flat", "not modelled"),
            (None, None, "mystery", "unknown fee type"),
        ],
    )
    def test_fails_closed(
        self,
        override_type: str | None,
        override_multiplier: Decimal | None,
        series_type: str,
        reason: str,
    ) -> None:
        event = replace(
            fixture_event("events_KXINX.json"),
            fee_type_override=override_type,
            fee_multiplier_override=override_multiplier,
        )
        series = SeriesInfo("KXINX", "", "", series_type, Decimal(1))
        resolution = resolve_fee_schedule(event, series)
        assert resolution.schedule is None
        assert reason in resolution.reason

    def test_unknown_series(self) -> None:
        resolution = resolve_fee_schedule(fixture_event("events_KXINX.json"), None)
        assert resolution.schedule is None
