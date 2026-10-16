"""Fallback across providers, and what to do when they disagree.

The chain tries providers in configured order and returns the first usable
answer. Two things it deliberately does *not* do:

It does not treat "the provider answered and has nothing" the same as "the
provider was unreachable". A delisted ticker and a timed-out request need
different responses — the first is a fact about the instrument that the next
provider will also report, the second is worth retrying elsewhere.

It does not silently prefer whichever source answered first when two
disagree materially. That is the tempting move and the wrong one: it hides a
data problem behind a number that looks fine. A disagreement beyond the
configured tolerance is recorded and surfaced, and the primary's value is
used with its state marked `degraded` so the UI can say so.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from akmarkets.providers.base import (
    Bar,
    DataState,
    Disagreement,
    HistoryProvider,
    ProviderResult,
    Quote,
    QuoteProvider,
)


class HistoryChain:
    """Daily bars from the first provider that has them."""

    def __init__(self, providers: Sequence[HistoryProvider]) -> None:
        if not providers:
            raise ValueError("a provider chain needs at least one provider")
        self.providers = list(providers)

    @property
    def name(self) -> str:
        return "+".join(provider.name for provider in self.providers)

    def fetch_history(self, symbol: str, start: date, end: date) -> ProviderResult[list[Bar]]:
        attempts: list[str] = []
        saw_definite_absence = False

        for provider in self.providers:
            result = provider.fetch_history(symbol, start, end)
            if result.ok and result.value:
                if attempts:
                    # Succeeded, but not on the primary. Worth saying: a
                    # chain that quietly runs on its backup for a month is a
                    # chain nobody knows is broken.
                    return ProviderResult(
                        value=result.value,
                        state=DataState.DEGRADED,
                        provenance=result.provenance,
                        message=f"fell back to {provider.name}; {'; '.join(attempts)}",
                    )
                return result
            if result.state is DataState.UNKNOWN:
                saw_definite_absence = True
            attempts.append(f"{provider.name}: {result.message or result.state.value}")

        # Every provider agreeing there is nothing is a different answer
        # from every provider being unreachable, and the health page needs
        # to distinguish them.
        state = DataState.UNKNOWN if saw_definite_absence else DataState.FAILED
        return ProviderResult(
            value=None, state=state, message=f"{symbol}: {'; '.join(attempts)}"
        )


class QuoteChain:
    """Delayed quotes, with cross-provider agreement checking.

    When more than one provider answers, closes are compared. The tolerance
    is a configured percentage rather than an absolute, because a cent of
    difference on a $3 stock means something quite different from a cent on
    a $3,000 one.
    """

    def __init__(
        self,
        providers: Sequence[QuoteProvider],
        disagreement_tolerance_percent: Decimal = Decimal("0.5"),
    ) -> None:
        if not providers:
            raise ValueError("a provider chain needs at least one provider")
        self.providers = list(providers)
        self.tolerance = disagreement_tolerance_percent

    @property
    def name(self) -> str:
        return "+".join(provider.name for provider in self.providers)

    def fetch_quotes(
        self, symbols: Sequence[str], cross_check: bool = False
    ) -> tuple[ProviderResult[dict[str, Quote]], list[Disagreement]]:
        attempts: list[str] = []
        primary: ProviderResult[dict[str, Quote]] | None = None
        others: list[tuple[str, dict[str, Quote]]] = []

        for provider in self.providers:
            result = provider.fetch_quotes(symbols)
            if result.value:
                if primary is None:
                    primary = result
                    if not cross_check:
                        break
                else:
                    others.append((provider.name, result.value))
                    continue
            else:
                attempts.append(f"{provider.name}: {result.message or result.state.value}")

        if primary is None or not primary.value:
            return (
                ProviderResult(
                    value=None,
                    state=DataState.FAILED,
                    message="; ".join(attempts) or "no provider returned a quote",
                ),
                [],
            )

        disagreements = (
            self._compare(primary.provenance.source if primary.provenance else "primary",
                          primary.value, others)
            if others
            else []
        )
        if disagreements:
            return (
                ProviderResult(
                    value=primary.value,
                    state=DataState.DEGRADED,
                    provenance=primary.provenance,
                    message=(
                        f"{len(disagreements)} symbol(s) disagree between providers by more "
                        f"than {self.tolerance}%"
                    ),
                ),
                disagreements,
            )
        return primary, []

    def _compare(
        self,
        primary_name: str,
        primary: dict[str, Quote],
        others: list[tuple[str, dict[str, Quote]]],
    ) -> list[Disagreement]:
        findings: list[Disagreement] = []
        for symbol, quote in primary.items():
            values = {primary_name: quote.price}
            for name, batch in others:
                other = batch.get(symbol)
                if other is not None:
                    values[name] = other.price
            if len(values) < 2:
                continue
            low, high = min(values.values()), max(values.values())
            if low <= 0:
                continue
            spread = (high - low) / low * Decimal(100)
            if spread > self.tolerance:
                findings.append(
                    Disagreement(
                        subject=symbol, metric="price", values=values, spread_percent=spread
                    )
                )
        return findings
