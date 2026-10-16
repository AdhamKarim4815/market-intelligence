"""Provider interfaces, freshness, and how a failure is represented.

`Bar`, `Quote` and `ReportedFact` are re-exported from
`akmarkets.domain.market` rather than defined here. They are facts about
a market, not artefacts of whoever fetched them, and defining them in an
adapter layer would force the indicator engine to import one.

No provider is load-bearing. Each data category declares a Protocol here;
adapters implement it; a chain tries them in configured order. Swapping a
source is a configuration change, not a rewrite, which matters because every
free data source eventually rate-limits, changes shape, or disappears.

Two rules run through everything below.

**A value carries its provenance.** Source, effective time and retrieval
time travel with the data, because "what is this number and how old is it"
has to be answerable at the point of display, not reconstructed later.

**A failure is a value.** Providers return a `ProviderResult` with a status
rather than raising. An exception forces every caller into a try/except that
usually swallows it, and a swallowed provider error becomes a blank on a
dashboard that looks like real data. A status has to be handled or rendered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from akmarkets.domain.market import Bar, Quote, ReportedFact


class DataState(StrEnum):
    """How much a value can be trusted right now.

    `UNKNOWN` exists so a component that has never reported cannot be
    mistaken for a healthy one. Absence of bad news is not good news.
    """

    HEALTHY = "healthy"
    DELAYED = "delayed"
    DEGRADED = "degraded"
    STALE = "stale"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def is_usable(self) -> bool:
        """Whether a value in this state may be shown, with its label."""
        return self in {DataState.HEALTHY, DataState.DELAYED, DataState.STALE}


@dataclass(frozen=True)
class Provenance:
    """Where a value came from and when.

    `effective_at` is when the fact was true in the world — a session close,
    a filing date. `retrieved_at` is when we fetched it. Conflating them is
    how a three-day-old close ends up rendered as a live quote.
    """

    source: str
    retrieved_at: datetime
    effective_at: datetime | None = None
    url: str | None = None

    @property
    def age_seconds(self) -> float:
        moment = self.effective_at or self.retrieved_at
        return (datetime.now(UTC) - moment).total_seconds()


@dataclass(frozen=True)
class ProviderResult[T]:
    """A value, or an explained absence. Never an exception across a boundary."""

    value: T | None
    state: DataState
    provenance: Provenance | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None and self.state.is_usable

    @classmethod
    def healthy(cls, value: T, provenance: Provenance) -> ProviderResult[T]:
        return cls(value=value, state=DataState.HEALTHY, provenance=provenance)

    @classmethod
    def failed(cls, source: str, message: str) -> ProviderResult[T]:
        return cls(
            value=None,
            state=DataState.FAILED,
            provenance=Provenance(source=source, retrieved_at=datetime.now(UTC)),
            message=message,
        )

    @classmethod
    def missing(cls, source: str, message: str = "no data") -> ProviderResult[T]:
        """The provider answered and genuinely has nothing.

        Distinct from `failed`, which means we could not ask. A delisted
        ticker and a timed-out request need different responses: one is a
        fact about the instrument, the other about the network.
        """
        return cls(
            value=None,
            state=DataState.UNKNOWN,
            provenance=Provenance(source=source, retrieved_at=datetime.now(UTC)),
            message=message,
        )


@dataclass(frozen=True)
class Disagreement:
    """Two providers materially disagreeing about the same figure.

    Recorded rather than resolved. Picking whichever answered first is the
    tempting move and the wrong one: it hides a data problem behind a number
    that looks fine.
    """

    subject: str
    # Named `metric` rather than `field`: as a dataclass attribute, `field`
    # shadows `dataclasses.field` for the rest of the class body, so the
    # default_factory on the next line stops working.
    metric: str
    values: dict[str, Decimal] = field(default_factory=dict)
    spread_percent: Decimal | None = None


@runtime_checkable
class QuoteProvider(Protocol):
    """Delayed or real-time last prices."""

    name: str

    def fetch_quotes(self, symbols: Sequence[str]) -> ProviderResult[dict[str, Quote]]: ...


@runtime_checkable
class HistoryProvider(Protocol):
    """Daily OHLCV history."""

    name: str

    def fetch_history(
        self, symbol: str, start: date, end: date
    ) -> ProviderResult[list[Bar]]: ...


@runtime_checkable
class FundamentalsProvider(Protocol):
    """Reported financials, keyed by the concepts the filer actually used."""

    name: str

    def fetch_fundamentals(self, symbol: str) -> ProviderResult[dict[str, object]]: ...


__all__ = [
    "Bar",
    "DataState",
    "Disagreement",
    "FundamentalsProvider",
    "HistoryProvider",
    "Provenance",
    "ProviderResult",
    "Quote",
    "QuoteProvider",
    "ReportedFact",
]
