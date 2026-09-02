"""Money and FX arithmetic must be exact. These tests are the reason to trust
every reconciliation number in the product."""

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

import pytest

from riskops.money import (
    Money,
    MoneyError,
    convert,
    exponent_of,
    minor_to_major_float,
    sum_money,
)


class TestConstruction:
    def test_minor_units_are_exact(self):
        assert Money(1250, "USD").as_decimal() == Decimal("12.50")
        assert Money(1250, "JPY").as_decimal() == Decimal("1250")
        assert Money(1250, "BHD").as_decimal() == Decimal("1.250")

    def test_from_major_string(self):
        assert Money.from_major("12.50", "USD").minor_units == 1250
        assert Money.from_major("1250", "JPY").minor_units == 1250
        assert Money.from_major("1.250", "BHD").minor_units == 1250

    def test_float_amounts_are_refused(self):
        # The whole point of the module: a float never becomes money.
        with pytest.raises(MoneyError):
            Money.from_major(12.50, "USD")
        with pytest.raises(MoneyError):
            Money(12.5, "USD")  # type: ignore[arg-type]

    def test_bool_is_not_an_int_here(self):
        with pytest.raises(MoneyError):
            Money(True, "USD")  # type: ignore[arg-type]

    def test_sub_minor_precision_is_refused_not_rounded(self):
        # 12.505 USD is not representable; silently rounding it is how a
        # reconciliation break gets invented.
        with pytest.raises(MoneyError, match="finer than"):
            Money.from_major("12.505", "USD")

    def test_zero_decimal_currency_rejects_fractions(self):
        with pytest.raises(MoneyError, match="finer than"):
            Money.from_major("1250.5", "JPY")

    def test_unknown_currency(self):
        with pytest.raises(MoneyError):
            Money(100, "XXX")
        with pytest.raises(MoneyError):
            exponent_of("")


class TestArithmetic:
    def test_the_classic_float_failure_does_not_happen(self):
        # 0.1 + 0.2 == 0.30000000000000004 in float. Not here.
        total = Money.from_major("0.10", "USD") + Money.from_major("0.20", "USD")
        assert total == Money.from_major("0.30", "USD")
        assert total.minor_units == 30

    def test_currency_mixing_is_refused(self):
        with pytest.raises(MoneyError, match="convert"):
            Money(100, "USD") + Money(100, "EUR")
        with pytest.raises(MoneyError):
            # Ordering across currencies is meaningless without a rate, so it
            # raises rather than silently comparing minor units.
            assert Money(100, "USD") > Money(100, "EUR")

    def test_sum_money_of_empty_list_still_types(self):
        assert sum_money([], "SGD") == Money(0, "SGD")

    def test_fee_split_always_reconciles(self):
        # Rounding is absorbed by the remainder, so share + remainder == whole
        # for every amount. If this ever fails, "fee mismatch" becomes noise.
        for minor in range(1, 2000):
            amount = Money(minor, "USD")
            share, remainder = amount.split_percent("2.9")
            assert share + remainder == amount

    def test_multiply_refuses_float_factor(self):
        with pytest.raises(MoneyError):
            Money(1000, "USD").multiply(0.029)


class TestConversion:
    def test_two_decimal_to_two_decimal(self):
        result = convert(Money.from_major("100.00", "USD"), "SGD", "1.3450")
        assert result.target == Money.from_major("134.50", "SGD")
        assert result.remainder == 0

    def test_two_decimal_to_zero_decimal(self):
        # USD 62.40 -> JPY at 151.23 = 9436.752 -> 9437 (half-even)
        result = convert(Money.from_major("62.40", "USD"), "JPY", "151.23")
        assert result.target == Money(9437, "JPY")
        assert result.remainder == Decimal("0.248")

    def test_two_decimal_to_three_decimal(self):
        result = convert(Money.from_major("100.00", "USD"), "BHD", "0.376")
        assert result.target == Money(37600, "BHD")

    def test_remainder_records_what_rounding_moved(self):
        result = convert(Money.from_major("10.00", "USD"), "JPY", "151.239")
        # 1512.39 -> 1512, so rounding removed 0.39 of a yen from the payer.
        assert result.target == Money(1512, "JPY")
        assert result.remainder == Decimal("-0.39")

    def test_rounding_mode_is_explicit_and_honoured(self):
        up = convert(Money.from_major("1.00", "USD"), "JPY", "150.9", rounding=ROUND_HALF_EVEN)
        down = convert(Money.from_major("1.00", "USD"), "JPY", "150.9", rounding=ROUND_DOWN)
        assert up.target == Money(151, "JPY")
        assert down.target == Money(150, "JPY")

    def test_float_rate_is_refused(self):
        with pytest.raises(MoneyError):
            convert(Money(100, "USD"), "SGD", 1.345)

    def test_non_positive_rate_is_refused(self):
        with pytest.raises(MoneyError):
            convert(Money(100, "USD"), "SGD", "0")

    def test_round_trip_is_not_assumed_lossless(self):
        # Converting out and back does not have to return the original amount.
        # Asserting that it does is a classic incorrect test; the honest
        # assertion is that the drift is bounded by one minor unit.
        forward = convert(Money.from_major("62.41", "USD"), "JPY", "151.23")
        back = convert(forward.target, "USD", str(Decimal(1) / Decimal("151.23")))
        assert abs(back.target.minor_units - 6241) <= 1


def test_display_helper_is_clearly_marked_and_only_for_display():
    assert minor_to_major_float(1250, "USD") == 12.5
    assert minor_to_major_float(1250, "JPY") == 1250.0
