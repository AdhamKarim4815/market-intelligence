"""Market observations: the facts a provider reports, as domain types.

A daily bar and a quote are facts about a market, not artefacts of whoever
fetched them. They live here so `akmarkets.domain` can reason about a price
series without importing an adapter — the layering rule is not decoration,
it is what keeps the indicator engine testable with no network at all.

`akmarkets.providers.base` re-exports these, so an adapter reads naturally
without the domain depending on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar.

    `adjusted_close` is optional and separate from `close` on purpose: an
    adjusted series is right for computing returns and wrong for showing
    what a share actually traded at, and silently substituting one for the
    other is how a chart stops matching a broker statement.
    """

    on: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    adjusted_close: Decimal | None = None


@dataclass(frozen=True)
class Quote:
    """A last price, with the honesty to say how delayed it is."""

    symbol: str
    price: Decimal
    previous_close: Decimal | None = None
    change: Decimal | None = None
    change_percent: Decimal | None = None
    volume: int | None = None
    currency: str = "USD"
    as_of: datetime | None = None
    is_delayed: bool = True


@dataclass(frozen=True)
class ReportedFact:
    """One figure as filed, with the document it came from.

    `concept` and `form` are part of the fact, not metadata about it:
    "revenue was $X" is not a claim anyone can check without knowing which
    accounting concept the filer used and whether the document was audited.
    """

    metric: str
    concept: str
    value: Decimal
    period_end: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None  # FY | Q1..Q4
    form: str = ""
    filed_on: date | None = None

    @property
    def is_annual(self) -> bool:
        return self.fiscal_period == "FY" or self.form.startswith(("10-K", "20-F"))
