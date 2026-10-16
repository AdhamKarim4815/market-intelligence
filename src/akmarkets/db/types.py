"""Column types that keep exact decimals exact on both backends.

Postgres has a real `NUMERIC` and round-trips a `Decimal` losslessly.
SQLite does not: it has no decimal type, so SQLAlchemy's `Numeric` on SQLite
stores a float and converts back on read, warning that it cannot preserve
the value. For a portfolio compared against a broker statement to the cent,
"cannot preserve the value" is disqualifying, and a difference in behaviour
between the local and production backends is worse than either.

So on SQLite these store the decimal's *string* form and parse it back.
That is exact on both sides and identical in Python.

The cost of that choice, stated plainly so nobody trips over it: on SQLite
these columns sort and aggregate lexicographically, so `ORDER BY amount` and
`SUM(amount)` in SQL are wrong. Nothing in this application does either --
the layering rule already puts all money arithmetic in `akmarkets.domain`,
which loads rows and sums `Decimal` in Python. `tests/test_layering.py`
keeps that true. If a future aggregate genuinely needs to happen in SQL, it
needs a Postgres-only code path, not a change to this type.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Dialect, Numeric, String, TypeDecorator

from akmarkets.domain.money import MONEY_PLACES, QUANTITY_PLACES

# Wide enough for any realistic portfolio total at full precision, with room
# for intermediate products of price and quantity.
_PRECISION = 28


class ExactDecimal(TypeDecorator[Decimal]):
    """A Decimal that survives the round trip on SQLite and Postgres alike."""

    impl = Numeric
    cache_ok = True

    def __init__(self, scale: int) -> None:
        self.scale = scale
        super().__init__(precision=_PRECISION, scale=scale, asdecimal=True)

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(48))
        return dialect.type_descriptor(Numeric(_PRECISION, self.scale, asdecimal=True))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        # `str` on a float first: Decimal(0.1) is not 0.1. See money.to_decimal.
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
        quantized = amount.quantize(Decimal(1).scaleb(-self.scale))
        return str(quantized) if dialect.name == "sqlite" else quantized

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


class MoneyColumn(ExactDecimal):
    """A monetary amount. Four decimal places -- see domain.money."""

    cache_ok = True

    def __init__(self) -> None:
        super().__init__(scale=MONEY_PLACES)


class QuantityColumn(ExactDecimal):
    """A share count. Eight decimal places, because brokers report that many."""

    cache_ok = True

    def __init__(self) -> None:
        super().__init__(scale=QUANTITY_PLACES)
