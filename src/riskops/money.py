"""Money and FX arithmetic.

The single rule of this module: **an amount of money is never a float.**

An amount is an integer count of minor units (cents, satang, yen) plus its
ISO-4217 currency. `Decimal` appears only where a ratio genuinely is decimal -
an FX rate or a fee percentage - and every such computation ends by quantising
back to an integer with an explicit rounding mode.

Why this matters here and not in an ordinary app: this project reconciles an
authorised amount against a captured amount against a settled amount, in two
currencies, across a fee split. `0.1 + 0.2 != 0.3` turns that into a
reconciliation break that does not exist, and a risk signal that fires on
nothing. Every FX conversion therefore also returns its *rounding remainder*,
so the cent that rounding creates or destroys is recorded rather than silently
absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

# ISO-4217 minor-unit exponents for the currencies this demo uses.
# JPY and KRW have no minor unit; BHD has three. Assuming "always 2" is the
# classic cross-border bug, so the exponent is data, not a constant.
CURRENCY_EXPONENTS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CNY": 2,
    "HKD": 2,
    "SGD": 2,
    "AUD": 2,
    "CAD": 2,
    "MYR": 2,
    "THB": 2,
    "PHP": 2,
    "IDR": 2,
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "BHD": 3,
}

SUPPORTED_CURRENCIES = tuple(sorted(CURRENCY_EXPONENTS))


class MoneyError(ValueError):
    """Raised when an amount or currency is not usable."""


def exponent_of(currency: str) -> int:
    """Minor-unit exponent for `currency`, or raise."""
    code = (currency or "").strip().upper()
    if code not in CURRENCY_EXPONENTS:
        raise MoneyError(f"unsupported currency {currency!r}; known: {SUPPORTED_CURRENCIES}")
    return CURRENCY_EXPONENTS[code]


@dataclass(frozen=True, order=False)
class Money:
    """An exact amount: `minor_units` of `currency`.

    `Money(1250, "USD")` is USD 12.50. `Money(1250, "JPY")` is JPY 1250.
    """

    minor_units: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.minor_units, int) or isinstance(self.minor_units, bool):
            raise MoneyError(
                f"minor_units must be an int, got {type(self.minor_units).__name__}. "
                "Floats are rejected on purpose - build money from minor units."
            )
        code = (self.currency or "").strip().upper()
        exponent_of(code)
        object.__setattr__(self, "currency", code)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_major(cls, major: str | int | Decimal, currency: str) -> Money:
        """Build from a major-unit string such as "12.50".

        A `float` is refused: `float("0.07")` is not 0.07 and the error would
        be invisible until it reached a reconciliation report.
        """
        if isinstance(major, float):
            raise MoneyError(
                "refusing to build Money from a float; pass a str or Decimal, "
                'e.g. Money.from_major("12.50", "USD")'
            )
        exponent = exponent_of(currency)
        try:
            value = Decimal(str(major))
        except InvalidOperation as exc:
            raise MoneyError(f"{major!r} is not a decimal amount") from exc
        scaled = value.scaleb(exponent)
        units = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
        if units != scaled:
            raise MoneyError(
                f"{major} {currency} is finer than the currency's minor unit "
                f"(10^-{exponent}); round it explicitly before constructing Money"
            )
        return cls(int(units), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    # -- presentation ------------------------------------------------------

    @property
    def exponent(self) -> int:
        return CURRENCY_EXPONENTS[self.currency]

    def as_decimal(self) -> Decimal:
        """Major units as an exact Decimal. For display and reporting only."""
        return Decimal(self.minor_units).scaleb(-self.exponent)

    def format(self) -> str:
        return f"{self.as_decimal():,.{self.exponent}f} {self.currency}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.format()

    # -- arithmetic --------------------------------------------------------

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise MoneyError(
                f"cannot combine {self.currency} and {other.currency} directly; "
                "convert with convert() first so the FX rate is recorded"
            )

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor_units, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.minor_units), self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor_units < other.minor_units

    def __le__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor_units <= other.minor_units

    def __gt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor_units > other.minor_units

    def __ge__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor_units >= other.minor_units

    def is_zero(self) -> bool:
        return self.minor_units == 0

    def multiply(self, factor: str | int | Decimal, rounding: str = ROUND_HALF_EVEN) -> Money:
        """Scale by a decimal factor (a fee rate, say), rounding to minor units."""
        if isinstance(factor, float):
            raise MoneyError("refusing to scale Money by a float; pass a str or Decimal")
        scaled = (Decimal(self.minor_units) * Decimal(str(factor))).to_integral_value(rounding=rounding)
        return Money(int(scaled), self.currency)

    def split_percent(self, percent: str | Decimal) -> tuple[Money, Money]:
        """Split into (share, remainder) so the two always sum back to self.

        Used for fee splits. The remainder absorbs the rounding, which is why
        the parts reconcile exactly and a "fee mismatch" signal means something.
        """
        share = self.multiply(Decimal(str(percent)) / Decimal(100))
        return share, self - share


@dataclass(frozen=True)
class Conversion:
    """The full record of one FX conversion - inputs, output and the remainder."""

    source: Money
    target: Money
    rate: Decimal
    rounding: str
    # What rounding threw away, in target minor units as an exact Decimal.
    # Positive means the payer was rounded up; negative means rounded down.
    remainder: Decimal

    def as_row(self) -> dict[str, object]:
        return {
            "source_amount_minor": self.source.minor_units,
            "source_currency": self.source.currency,
            "target_amount_minor": self.target.minor_units,
            "target_currency": self.target.currency,
            "fx_rate": str(self.rate),
            "rounding_mode": self.rounding,
            "rounding_remainder_minor": str(self.remainder),
        }


def convert(
    amount: Money,
    to_currency: str,
    rate: str | Decimal,
    rounding: str = ROUND_HALF_EVEN,
) -> Conversion:
    """Convert `amount` at `rate` (target units per source unit).

    The exponents of the two currencies are applied explicitly, so
    USD -> JPY (2 -> 0) and USD -> BHD (2 -> 3) are both correct without a
    special case.
    """
    if isinstance(rate, float):
        raise MoneyError("refusing to convert at a float rate; pass a str or Decimal")
    quote = Decimal(str(rate))
    if quote <= 0:
        raise MoneyError(f"fx rate must be positive, got {quote}")

    target_exponent = exponent_of(to_currency)
    exact = (
        Decimal(amount.minor_units).scaleb(-amount.exponent) * quote
    ).scaleb(target_exponent)
    units = exact.to_integral_value(rounding=rounding)
    return Conversion(
        source=amount,
        target=Money(int(units), to_currency),
        rate=quote,
        rounding=rounding,
        remainder=units - exact,
    )


def sum_money(amounts: list[Money], currency: str) -> Money:
    """Total a list, with an explicit currency so an empty list still types."""
    total = Money.zero(currency)
    for item in amounts:
        total = total + item
    return total


def minor_to_major_float(minor_units: int, currency: str) -> float:
    """Major units as a float - **for charting and display only.**

    Named loudly so it can be grepped. It must never appear in a comparison,
    a reconciliation, or anything that produces a risk signal.
    """
    return float(Decimal(minor_units).scaleb(-exponent_of(currency)))
