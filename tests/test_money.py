"""Exact decimal arithmetic. The floor everything financial stands on."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from akmarkets.domain.money import (
    MoneyError,
    format_money,
    format_quantity,
    is_zero_quantity,
    money,
    percent_change,
    quantity,
    to_decimal,
)


def test_float_input_takes_the_decimal_the_source_meant() -> None:
    """`Decimal(0.1)` is not 0.1, and provider JSON hands us floats.

    This is the single most consequential line in the money module: routing
    a float through `str` first is the difference between a cost basis that
    matches a broker statement and one that drifts by cents over a year.
    """
    assert to_decimal(0.1) == Decimal("0.1")
    # The lint that flags this is right in general and wrong here: the whole
    # point is that the float constructor gives a different number.
    assert to_decimal(0.1) != Decimal(0.1)  # noqa: RUF032


def test_repeated_addition_stays_exact() -> None:
    """Ten times a tenth is one, and three tenths is three tenths.

    The second case is the one that bites: `0.1 + 0.2` in binary floating
    point is 0.30000000000000004, and a cost basis built by adding lot
    values inherits that error permanently.
    """
    assert sum((money(0.1) for _ in range(10)), start=Decimal("0")) == Decimal("1.0000")
    assert money(0.1) + money(0.2) == Decimal("0.3000")
    assert 0.1 + 0.2 != 0.3


def test_strings_from_a_form_are_accepted() -> None:
    assert money("$1,234.56") == Decimal("1234.5600")
    assert money(" 42 ") == Decimal("42.0000")


def test_booleans_are_rejected_rather_than_coerced() -> None:
    """`bool` is an `int` subclass; a True reaching a price field is a bug."""
    with pytest.raises(MoneyError):
        to_decimal(True)


@pytest.mark.parametrize("value", ["", "abc", None, object()])
def test_uninterpretable_input_raises(value: object) -> None:
    with pytest.raises(MoneyError):
        to_decimal(value)


def test_money_rounds_half_to_even() -> None:
    """Banker's rounding: HALF_UP biases a long series of roundings upward."""
    assert money("0.00005") == Decimal("0.0000")
    assert money("0.00015") == Decimal("0.0002")


def test_quantity_keeps_eight_places() -> None:
    assert quantity("0.123456789") == Decimal("0.12345679")


def test_lot_closure_tolerates_split_dust_but_not_a_real_share() -> None:
    """A third of a share three ways leaves a repeating remainder.

    Exact `== 0` would leave a closed position permanently open by 1e-17
    shares. A genuine 1e-8 share, which a broker can really report, must
    still count as held.
    """
    assert is_zero_quantity(Decimal("1E-17"))
    assert is_zero_quantity(Decimal("0"))
    assert not is_zero_quantity(Decimal("1E-8"))


def test_percent_change_is_undefined_against_a_zero_base() -> None:
    """Undefined is None, never a large number and never zero."""
    assert percent_change(Decimal("10"), Decimal("0")) is None
    assert percent_change(Decimal("10"), None) is None
    assert percent_change(None, Decimal("10")) is None
    assert percent_change(Decimal("11"), Decimal("10")) == Decimal("0.1")


def test_missing_values_render_as_a_dash_not_a_zero() -> None:
    """A position with no known price is not a position worth nothing."""
    assert format_money(None) == "—"
    assert format_quantity(None) == "—"
    assert format_money(Decimal("0")) == "$0.00"


def test_money_formatting_puts_the_sign_before_the_symbol() -> None:
    assert format_money(Decimal("-1234.5")) == "-$1,234.50"
    assert format_money(Decimal("1234.5")) == "$1,234.50"


def test_quantity_formatting_does_not_use_scientific_notation() -> None:
    """`Decimal.normalize()` renders 100 as 1E+2; a share count must not."""
    assert format_quantity(Decimal("100.00000000")) == "100"
    assert format_quantity(Decimal("1.50000000")) == "1.5"


@given(st.decimals(min_value=-10**9, max_value=10**9, places=4))
def test_money_quantisation_is_idempotent(value: Decimal) -> None:
    assert money(money(value)) == money(value)


@given(
    st.lists(st.decimals(min_value=-10**6, max_value=10**6, places=4), min_size=1, max_size=50)
)
def test_summation_order_does_not_change_the_total(values: list[Decimal]) -> None:
    """Portfolio value is a sum over positions in whatever order they load.

    With floats this property fails; that is the bug this module exists to
    prevent.
    """
    forward = sum(values, start=Decimal("0"))
    backward = sum(reversed(values), start=Decimal("0"))
    assert forward == backward
