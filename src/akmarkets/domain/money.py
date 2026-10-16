"""Exact decimal arithmetic for money and share quantities.

`float` is wrong for this application and the reason is not academic. A
portfolio value is a sum of hundreds of products of a price and a quantity;
binary floating point cannot represent 0.1, so those errors accumulate in a
figure the user will compare against a broker statement to the cent. Worse,
a tax-lot engine decides *whether a lot is closed* by comparing a remaining
quantity to zero, and a residue of 1e-17 shares turns a closed position into
an open one that never disappears.

So every monetary amount and every share quantity is a `Decimal`, end to
end: parsed as `Decimal`, stored as `Decimal`, summed as `Decimal`, and only
converted to a string for display. The two quantisation levels below are the
only places rounding is allowed to happen.

Rounding is `ROUND_HALF_EVEN` (banker's rounding), which is what financial
systems use: `ROUND_HALF_UP` biases a long series of roundings upward, and a
portfolio does a lot of rounding.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final

# Money is carried at four places rather than two. Intermediate values --
# per-share fees, dividend rates, an average cost -- routinely need more
# precision than the currency's minor unit, and rounding to cents at every
# step is how a cost basis drifts. Presentation rounds to two; storage does
# not.
MONEY_PLACES: Final = 4
MONEY_QUANTUM: Final = Decimal(1).scaleb(-MONEY_PLACES)

# Brokers report fractional shares to eight places.
QUANTITY_PLACES: Final = 8
QUANTITY_QUANTUM: Final = Decimal(1).scaleb(-QUANTITY_PLACES)

ZERO: Final = Decimal("0")

# Below this, a residual share count is rounding dust from a split ratio or
# an average-cost allocation, not a real holding. Chosen an order of
# magnitude under the storage precision so a genuine 1e-8 share -- which a
# broker can actually report -- is never swallowed.
QUANTITY_EPSILON: Final = Decimal("1E-9")


class MoneyError(ValueError):
    """A value that cannot be interpreted as an exact decimal amount."""


def to_decimal(value: object) -> Decimal:
    """Coerce user or provider input to an exact Decimal.

    Floats are routed through `str` on purpose. `Decimal(0.1)` is
    0.1000000000000000055511151231257827, which is the true value of the
    float and almost never what the source meant; `Decimal(str(0.1))` is
    `0.1`, which is. Provider JSON gives us floats whether we like it or
    not, so the coercion belongs here rather than at each call site.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; almost certainly a bug
        raise MoneyError(f"cannot interpret {value!r} as a decimal amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
        if not text:
            raise MoneyError("cannot interpret an empty string as a decimal amount")
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"cannot interpret {value!r} as a decimal amount") from exc
    raise MoneyError(f"cannot interpret {value!r} as a decimal amount")


def money(value: object) -> Decimal:
    """A monetary amount quantised to storage precision."""
    return quantize_money(to_decimal(value))


def quantity(value: object) -> Decimal:
    """A share quantity quantised to storage precision."""
    return quantize_quantity(to_decimal(value))


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def quantize_quantity(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_QUANTUM, rounding=ROUND_HALF_EVEN)


def is_zero_quantity(value: Decimal) -> bool:
    """Whether a share count is zero for the purpose of closing a lot.

    Exact `== 0` is the wrong test after a 3-for-1 split or an average-cost
    allocation: the true remainder is a repeating decimal, and what survives
    quantisation is dust. See `QUANTITY_EPSILON`.
    """
    return abs(value) < QUANTITY_EPSILON


def format_money(value: Decimal | None, currency: str = "USD", places: int = 2) -> str:
    """Render for display. Never used as an input to further arithmetic.

    Returns an explicit dash for a missing value rather than a zero: a
    position with no known price and a position worth nothing are different
    facts, and the product is required not to conflate them.
    """
    if value is None:
        return "—"
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    sign = "-" if rounded < 0 else ""
    return f"{sign}{symbol}{abs(rounded):,.{places}f}"


def format_quantity(value: Decimal | None) -> str:
    """Render a share count without trailing zero noise."""
    if value is None:
        return "—"
    normalized = value.normalize()
    # `normalize()` renders trailing-zero integers in scientific notation --
    # Decimal("100.00000000").normalize() is Decimal("1E+2") -- so a whole
    # share count is routed through int() to stop a hundred shares reading
    # as an exponent.
    if normalized == normalized.to_integral_value():
        return f"{int(normalized):,}"
    return f"{normalized:,f}"


def percent_change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    """Proportional change, or None when it is not defined.

    Returns None rather than zero or infinity when the base is zero or
    either side is missing. A percentage against a zero base is not a large
    number, it is an undefined one, and rendering it as a number is exactly
    the kind of fabricated precision this project forbids.
    """
    if current is None or previous is None or previous == ZERO:
        return None
    return (current - previous) / previous
