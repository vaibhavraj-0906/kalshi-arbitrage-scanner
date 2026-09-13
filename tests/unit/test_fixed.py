from __future__ import annotations

import pytest

from karb.core.fixed import Cash, FixedPointError, Price, Qty, Rounding, div_round


class TestPriceParsing:
    def test_four_decimals(self) -> None:
        assert Price.parse("0.1800").raw == 1800

    def test_trailing_zeros_beyond_scale_are_exact(self) -> None:
        # Responses may emit six decimals; trailing zeros lose nothing.
        assert Price.parse("0.180000") == Price(1800)

    def test_inexact_value_is_rejected_not_rounded(self) -> None:
        with pytest.raises(FixedPointError, match="not exact"):
            Price.parse("0.18005")

    def test_bounds(self) -> None:
        assert Price.parse("1.0000") == Price.ONE
        assert Price.parse("0") == Price.ZERO
        with pytest.raises(FixedPointError):
            Price.parse("1.0001")

    @pytest.mark.parametrize("text", ["-0.01", "abc", "", "1e-4", " 0.5", ".5", "0,5"])
    def test_malformed(self, text: str) -> None:
        with pytest.raises(FixedPointError):
            Price.parse(text)

    def test_complement(self) -> None:
        assert Price.parse("0.8200").complement() == Price.parse("0.1800")


class TestQtyAndCash:
    def test_fractional_contracts(self) -> None:
        assert Qty.parse("1115.01").raw == 111501
        assert Qty.parse("10") == Qty.contracts(10)
        with pytest.raises(FixedPointError):
            Qty.parse("2.505")

    def test_negative_quantity_rejected(self) -> None:
        with pytest.raises(FixedPointError):
            Qty(1) - Qty(2)

    def test_notional_is_exact(self) -> None:
        # 0.18 x 1115.01 = 200.7018 dollars, exactly representable in micro-dollars.
        assert Price.parse("0.1800").notional(Qty.parse("1115.01")) == Cash(200_701_800)

    def test_payout_is_one_dollar_per_contract(self) -> None:
        assert Qty.parse("2.50").payout() == Cash.parse("2.5")

    def test_signed_cash(self) -> None:
        assert Cash.parse("-0.055000").raw == -55_000
        assert (-Cash(5)).raw == -5

    def test_units_do_not_mix(self) -> None:
        with pytest.raises(TypeError):
            Qty(1) + Cash(1)  # type: ignore[operator]
        with pytest.raises(TypeError):
            Price(1).notional(Cash(1))  # type: ignore[arg-type]

    def test_bool_and_float_raws_rejected(self) -> None:
        with pytest.raises(TypeError):
            Price(True)
        with pytest.raises(TypeError):
            Cash(1.0)  # type: ignore[arg-type]

    def test_total(self) -> None:
        assert Cash.total([Cash(1), Cash(-3), Cash(10)]) == Cash(8)

    def test_display(self) -> None:
        assert Cash(1_234_567).dollars() == "$1.23"
        assert Cash(-15_000).dollars() == "-$0.02"
        assert Cash(-5_000).dollars() == "$0.00"  # half-even to zero; no negative zero
        assert str(Cash(-55_000)) == "-0.055000"


class TestDivRound:
    @pytest.mark.parametrize(
        ("numerator", "denominator", "rounding", "expected"),
        [
            (7, 2, Rounding.FLOOR, 3),
            (7, 2, Rounding.CEIL, 4),
            (-7, 2, Rounding.FLOOR, -4),
            (-7, 2, Rounding.CEIL, -3),
            (5, 2, Rounding.HALF_EVEN, 2),
            (7, 2, Rounding.HALF_EVEN, 4),
            (-5, 2, Rounding.HALF_EVEN, -2),
            (-7, 2, Rounding.HALF_EVEN, -4),
            (8, 2, Rounding.CEIL, 4),
        ],
    )
    def test_cases(
        self, numerator: int, denominator: int, rounding: Rounding, expected: int
    ) -> None:
        assert div_round(numerator, denominator, rounding) == expected

    def test_denominator_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            div_round(1, 0, Rounding.FLOOR)

    def test_round_to_balance_unit(self) -> None:
        assert Cash(-58_639).round_to(10_000, Rounding.FLOOR) == Cash(-60_000)
        assert Cash(58_639).round_to(10_000, Rounding.CEIL) == Cash(60_000)
