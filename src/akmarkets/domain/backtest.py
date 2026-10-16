"""Strategy Lab: measuring whether a signal was worth anything.

This module exists so the rest of the platform can stop guessing. Every
threshold in the news pipeline, every weight in the attention score, and the
whole claim that "this is worth your attention" are currently *stated*
rather than *measured*, and `docs/CALIBRATION.md` says so. This is where
they become answerable.

**Look-ahead is the failure mode, and it is silent.** A backtest that peeks
produces a beautiful equity curve and teaches nothing. Three guards:

1. `AsOf` refuses to return any observation dated after its cut-off, and
   raises rather than filtering when handed one — a filter would hide the
   caller's bug.
2. Forward returns are computed from bars strictly *after* the signal date,
   never including it.
3. A signal on the final bar produces no outcome at all, rather than a
   partial one measured over however many days happen to remain.

**Nothing here reports a p-value.** With a handful of signals over one
universe, a significance test would be a decoration on a sample too small to
support it. What is reported instead is the count, the spread, and the hit
rate — from which a reader can see for themselves that eleven observations
prove nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from akmarkets.domain.market import Bar
from akmarkets.domain.money import ZERO

# The horizons every evaluation reports. Fixed rather than configurable so
# results from different runs are comparable — a strategy evaluated at 7
# days and one at 5 are not two measurements of the same thing.
HORIZONS: tuple[int, ...] = (1, 5, 20, 60, 120)

# Below this, a result is reported but explicitly marked as too small to
# mean anything. Thirty is not a threshold of significance; it is the point
# below which a hit rate is obviously just the last few coin flips.
MIN_SAMPLE = 30


class LookAheadError(RuntimeError):
    """Something asked for data it could not have known at the time."""


@dataclass(frozen=True)
class AsOf:
    """A cut-off that refuses to hand back the future.

    Raising rather than filtering is deliberate. A silent filter means a
    caller with a bug gets a plausible answer and never learns; the whole
    point of a historical replay is that it cannot cheat, and a guard that
    quietly tidies up is not a guard.
    """

    on: date

    def check(self, observed: date, what: str = "observation") -> None:
        if observed > self.on:
            raise LookAheadError(
                f"{what} dated {observed} is after the as-of date {self.on}"
            )

    def bars(self, series: Sequence[Bar]) -> list[Bar]:
        """Only the bars that existed by the cut-off."""
        return [bar for bar in series if bar.on <= self.on]

    def latest(self, series: Sequence[Bar]) -> Bar | None:
        visible = self.bars(series)
        return visible[-1] if visible else None


@dataclass(frozen=True)
class Signal:
    """One dated observation to be evaluated."""

    symbol: str
    on: date
    label: str = ""


@dataclass(frozen=True)
class Outcome:
    """What happened after a signal, per horizon.

    `benchmark` is carried alongside so `excess` means something. A raw
    return over a rising market is not evidence a signal worked.
    """

    signal: Signal
    entry_price: Decimal
    returns: dict[int, Decimal]
    benchmark_returns: dict[int, Decimal]

    def excess(self, horizon: int) -> Decimal | None:
        mine = self.returns.get(horizon)
        theirs = self.benchmark_returns.get(horizon)
        if mine is None:
            return None
        return mine - theirs if theirs is not None else mine


def forward_returns(
    series: Sequence[Bar], signal_on: date, horizons: Sequence[int] = HORIZONS
) -> tuple[Decimal | None, dict[int, Decimal]]:
    """Entry price and returns over each horizon after a signal.

    Entry is the close *on* the signal date — the earliest price a decision
    made that day could realistically have achieved. Outcomes are measured
    from bars strictly after it.

    A horizon extending past the end of the data produces no entry for that
    horizon rather than a shortened one. A "120-day return" measured over 40
    days is not a rough answer; it is a different measurement wearing the
    wrong label.
    """
    index = next(
        (position for position, bar in enumerate(series) if bar.on == signal_on), None
    )
    if index is None or series[index].close == ZERO:
        return None, {}

    entry = series[index].close
    results: dict[int, Decimal] = {}
    for horizon in horizons:
        target = index + horizon
        if target >= len(series):
            continue
        results[horizon] = (series[target].close - entry) / entry
    return entry, results


def evaluate(
    signals: Sequence[Signal],
    series_by_symbol: Mapping[str, Sequence[Bar]],
    benchmark: Sequence[Bar] | None = None,
    horizons: Sequence[int] = HORIZONS,
) -> list[Outcome]:
    """Measure what happened after each signal."""
    outcomes: list[Outcome] = []
    for signal in signals:
        series = series_by_symbol.get(signal.symbol)
        if not series:
            continue
        entry, results = forward_returns(series, signal.on, horizons)
        if entry is None or not results:
            continue
        _, market = (
            forward_returns(benchmark, signal.on, horizons)
            if benchmark
            else (None, {})
        )
        outcomes.append(
            Outcome(
                signal=signal,
                entry_price=entry,
                returns=results,
                benchmark_returns=market,
            )
        )
    return outcomes


@dataclass(frozen=True)
class HorizonResult:
    """What a signal did at one horizon, with the sample size in view."""

    horizon: int
    count: int
    mean_excess: Decimal | None
    median_excess: Decimal | None
    beat_rate: Decimal | None
    best: Decimal | None
    worst: Decimal | None

    @property
    def is_meaningful(self) -> bool:
        return self.count >= MIN_SAMPLE

    def describe(self) -> str:
        if self.mean_excess is None:
            return f"{self.horizon}d: no outcomes"
        note = "" if self.is_meaningful else "  (too few to mean anything)"
        return (
            f"{self.horizon}d: n={self.count} mean {self.mean_excess:+.2%} "
            f"median {self.median_excess:+.2%} beat {self.beat_rate:.0%}{note}"
        )


def summarise(
    outcomes: Sequence[Outcome], horizons: Sequence[int] = HORIZONS
) -> list[HorizonResult]:
    """Aggregate outcomes per horizon.

    Median is reported alongside mean because one outcome can carry a mean
    entirely, and with samples this small that is the normal case rather
    than the exception. The beat rate is reported for the same reason: it
    says how *often* rather than how much.
    """
    results: list[HorizonResult] = []
    for horizon in horizons:
        excesses = [
            outcome.excess(horizon)
            for outcome in outcomes
            if outcome.excess(horizon) is not None
        ]
        clean = [value for value in excesses if value is not None]
        if not clean:
            results.append(
                HorizonResult(horizon, 0, None, None, None, None, None)
            )
            continue
        ordered = sorted(clean)
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / Decimal(2)
        )
        wins = sum(1 for value in clean if value > ZERO)
        results.append(
            HorizonResult(
                horizon=horizon,
                count=len(clean),
                mean_excess=sum(clean, start=ZERO) / Decimal(len(clean)),
                median_excess=median,
                beat_rate=Decimal(wins) / Decimal(len(clean)),
                best=ordered[-1],
                worst=ordered[0],
            )
        )
    return results


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayStep:
    on: date
    signals: tuple[Signal, ...]


def replay(
    dates: Sequence[date],
    series_by_symbol: Mapping[str, Sequence[Bar]],
    rule: Callable[[str, list[Bar], AsOf], bool],
) -> list[ReplayStep]:
    """Walk forward through history, applying a rule with no hindsight.

    The rule is handed only the bars visible at each step, and an `AsOf` it
    can use to check anything else it consults. It cannot reach the full
    series because it is never given it — which is a stronger guarantee
    than asking it not to look.
    """
    steps: list[ReplayStep] = []
    for on in sorted(dates):
        cut = AsOf(on)
        fired = [
            Signal(symbol=symbol, on=on)
            for symbol, series in series_by_symbol.items()
            if (visible := cut.bars(series)) and rule(symbol, visible, cut)
        ]
        steps.append(ReplayStep(on=on, signals=tuple(fired)))
    return steps


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationBucket:
    """Predicted confidence against what actually happened."""

    lower: Decimal
    upper: Decimal
    count: int
    predicted: Decimal
    observed: Decimal | None

    @property
    def gap(self) -> Decimal | None:
        if self.observed is None:
            return None
        return self.observed - self.predicted

    @property
    def is_meaningful(self) -> bool:
        return self.count >= MIN_SAMPLE


def calibration(
    predictions: Sequence[tuple[Decimal, bool]], buckets: int = 5
) -> list[CalibrationBucket]:
    """Compare stated confidence with observed frequency.

    If the platform says 80% a hundred times, roughly eighty of those should
    happen. This is the only thing that makes a confidence number mean
    anything, and until it has run, every confidence this system emits is a
    label rather than a measurement.

    Buckets below the minimum sample are returned and marked rather than
    hidden — a calibration chart that silently drops its thin buckets looks
    far better calibrated than the system actually is.
    """
    if not predictions:
        return []

    width = Decimal(1) / Decimal(buckets)
    results: list[CalibrationBucket] = []
    for index in range(buckets):
        lower = width * Decimal(index)
        upper = lower + width
        inside = [
            (confidence, happened)
            for confidence, happened in predictions
            if lower <= confidence < upper or (index == buckets - 1 and confidence == upper)
        ]
        if not inside:
            results.append(
                CalibrationBucket(lower, upper, 0, (lower + upper) / Decimal(2), None)
            )
            continue
        predicted = sum((value for value, _ in inside), start=ZERO) / Decimal(len(inside))
        observed = Decimal(sum(1 for _, happened in inside if happened)) / Decimal(
            len(inside)
        )
        results.append(
            CalibrationBucket(
                lower=lower,
                upper=upper,
                count=len(inside),
                predicted=predicted,
                observed=observed,
            )
        )
    return results
