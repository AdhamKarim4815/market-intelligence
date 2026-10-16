"""Technical indicators, computed exactly and stated honestly.

Every function here is deterministic `Decimal` arithmetic over a bar series.
None of them predict anything; they describe where a price is relative to
its own recent history, which is all a technical indicator has ever done.

Three rules run through the module.

**Insufficient data returns `None`, never a partial answer.** A 200-day
moving average computed from 40 bars is not a rough 200-day average, it is a
40-day average wearing the wrong label, and it would be indistinguishable
from the real thing on a chart.

**Series must be in ascending date order**, and it is checked rather than
assumed. A reversed series produces plausible numbers that are exactly
backwards — the single most dangerous failure available here, because
nothing about the output looks wrong.

**Nothing is annualised or extrapolated.** ATR is an average true range over
N bars, not a volatility forecast.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from akmarkets.domain.market import Bar
from akmarkets.domain.money import ZERO, quantize_money

# Wilder's smoothing constant for RSI and ATR. 14 is his original period and
# is kept because every published reference value assumes it; changing it
# silently would make this module's output uncomparable with anything else.
RSI_PERIOD = 14
ATR_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9


class SeriesOrderError(ValueError):
    """A bar series was not in ascending date order."""


def require_ascending(bars: Sequence[Bar]) -> None:
    """Refuse a series that runs backwards.

    Checked rather than sorted, because a caller handing over a reversed
    series has a bug somewhere upstream and silently fixing it here would
    hide the cause. One provider already returns newest-first.
    """
    for earlier, later in pairwise(bars):
        if later.on < earlier.on:
            raise SeriesOrderError(
                f"bars must be oldest-first; {later.on} follows {earlier.on}"
            )


def closes(bars: Sequence[Bar]) -> list[Decimal]:
    return [bar.close for bar in bars]


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Simple moving average, or None if there is not enough history."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window, start=ZERO) / Decimal(period)


def ema_series(values: Sequence[Decimal], period: int) -> list[Decimal]:
    """Exponential moving average over the whole series.

    Seeded with the simple average of the first `period` values rather than
    the first value alone. Seeding on one point makes the early EMA chase
    that single number, and the distortion persists for several periods —
    which matters because MACD is a difference of two EMAs and the error
    does not cancel.
    """
    if period <= 0 or len(values) < period:
        return []
    multiplier = Decimal(2) / Decimal(period + 1)
    seed = sum(values[:period], start=ZERO) / Decimal(period)
    result = [seed]
    for value in values[period:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(values: Sequence[Decimal], period: int = RSI_PERIOD) -> Decimal | None:
    """Relative strength index, using Wilder's smoothing.

    Returns 100 when there has been no down move in the window, which is
    correct and worth stating: RSI is undefined by division there, and the
    limit as losses approach zero is 100.
    """
    if len(values) < period + 1:
        return None

    changes = [later - earlier for earlier, later in pairwise(values)]
    gains = [change if change > 0 else ZERO for change in changes]
    losses = [-change if change < 0 else ZERO for change in changes]

    average_gain = sum(gains[:period], start=ZERO) / Decimal(period)
    average_loss = sum(losses[:period], start=ZERO) / Decimal(period)

    # Wilder's smoothing: each later value is a weighted blend, not a fresh
    # window. Using a rolling simple average instead is a common and subtle
    # error that produces a similar-looking but different indicator.
    for gain, loss in zip(gains[period:], losses[period:], strict=False):
        average_gain = (average_gain * Decimal(period - 1) + gain) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + loss) / Decimal(period)

    if average_loss == ZERO:
        return Decimal(100)
    strength = average_gain / average_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + strength))


@dataclass(frozen=True)
class Macd:
    line: Decimal
    signal: Decimal
    histogram: Decimal


def macd(
    values: Sequence[Decimal],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> Macd | None:
    """MACD line, signal and histogram.

    The two EMAs are aligned before subtracting: the fast one has more
    points because it needed less warm-up, and subtracting them
    position-by-position without trimming compares different dates — which
    produces a plausible line that is silently shifted in time.
    """
    fast_series = ema_series(values, fast)
    slow_series = ema_series(values, slow)
    if not fast_series or not slow_series:
        return None

    aligned = min(len(fast_series), len(slow_series))
    line_series = [
        fast_value - slow_value
        for fast_value, slow_value in zip(
            fast_series[-aligned:], slow_series[-aligned:], strict=True
        )
    ]
    signal_series = ema_series(line_series, signal)
    if not signal_series:
        return None

    line = line_series[-1]
    signal_value = signal_series[-1]
    return Macd(line=line, signal=signal_value, histogram=line - signal_value)


def true_range(current: Bar, previous: Bar | None) -> Decimal:
    """The greater of the day's range and its gap from the previous close.

    Using high−low alone understates a day that gapped: the price moved
    through the gap, it simply did not trade there.
    """
    span = current.high - current.low
    if previous is None:
        return span
    return max(
        span,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def atr(bars: Sequence[Bar], period: int = ATR_PERIOD) -> Decimal | None:
    """Average true range. A measure of realised range, not a forecast."""
    if len(bars) < period + 1:
        return None
    ranges = [
        true_range(bar, bars[index - 1] if index else None)
        for index, bar in enumerate(bars)
    ]
    average = sum(ranges[1 : period + 1], start=ZERO) / Decimal(period)
    for value in ranges[period + 1 :]:
        average = (average * Decimal(period - 1) + value) / Decimal(period)
    return average


def relative_volume(bars: Sequence[Bar], period: int = 20) -> Decimal | None:
    """Latest volume as a multiple of its recent average.

    None rather than a ratio when the average is zero: a stock that has not
    traded for twenty sessions has no baseline, and "infinite relative
    volume" is not a fact about it.
    """
    if len(bars) < period + 1:
        return None
    window = [Decimal(bar.volume) for bar in bars[-period - 1 : -1]]
    average = sum(window, start=ZERO) / Decimal(len(window))
    if average == ZERO:
        return None
    return Decimal(bars[-1].volume) / average


def gap_percent(bars: Sequence[Bar]) -> Decimal | None:
    """Today's open against yesterday's close."""
    if len(bars) < 2 or bars[-2].close == ZERO:
        return None
    return (bars[-1].open - bars[-2].close) / bars[-2].close


def drawdown(values: Sequence[Decimal]) -> Decimal | None:
    """Current distance below the running peak, as a negative proportion."""
    if not values:
        return None
    peak = max(values)
    if peak == ZERO:
        return None
    return (values[-1] - peak) / peak


def max_drawdown(values: Sequence[Decimal]) -> Decimal | None:
    """The worst peak-to-trough fall in the series.

    Computed forward with a running peak rather than as `min − max`, which
    would happily report a trough that occurred *before* its peak — a
    drawdown that never happened.
    """
    if len(values) < 2:
        return None
    peak = values[0]
    worst = ZERO
    for value in values:
        peak = max(peak, value)
        if peak > ZERO:
            worst = min(worst, (value - peak) / peak)
    return worst


# ---------------------------------------------------------------------------
# levels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Level:
    """A price level, with what makes it one.

    `touches` is the evidence. A level that a price approached once is a
    coincidence; one it turned at four times is a level. Publishing the
    count is what stops this being an assertion.
    """

    price: Decimal
    touches: int
    kind: str  # support | resistance

    @property
    def is_meaningful(self) -> bool:
        return self.touches >= 3


def _pivots(bars: Sequence[Bar], reach: int, high: bool) -> list[Decimal]:
    """Local extremes with `reach` bars either side that do not exceed them.

    Requiring both sides is what makes it a turning point rather than the
    end of the data — the last N bars can never be pivots, which is correct
    and occasionally surprising.
    """
    found: list[Decimal] = []
    for index in range(reach, len(bars) - reach):
        window = bars[index - reach : index + reach + 1]
        value = bars[index].high if high else bars[index].low
        others = [bar.high if high else bar.low for bar in window]
        if (high and value == max(others)) or (not high and value == min(others)):
            found.append(value)
    return found


def levels(
    bars: Sequence[Bar],
    reach: int = 5,
    tolerance: Decimal = Decimal("0.02"),
    limit: int = 3,
) -> tuple[list[Level], list[Level]]:
    """Support and resistance from clustered pivot points.

    The methodology, stated so it can be argued with and tested: find local
    highs and lows with `reach` bars either side, cluster them within
    `tolerance` of each other, and report clusters by how many pivots they
    contain. Nothing is smoothed, fitted or hand-placed.

    This is *not* a prediction and the type says so — `Level.is_meaningful`
    is a count of touches, not a probability that the price will turn there.
    A level with two touches is returned and marked unmeaningful rather than
    hidden, because the alternative is a UI that silently shows only the
    levels that happen to look convincing.
    """
    require_ascending(bars)
    if len(bars) < reach * 2 + 1:
        return [], []

    latest = bars[-1].close

    def cluster(points: Sequence[Decimal], kind: str) -> list[Level]:
        remaining = sorted(points)
        clusters: list[list[Decimal]] = []
        for point in remaining:
            for group in clusters:
                if abs(point - group[0]) / group[0] <= tolerance if group[0] else False:
                    group.append(point)
                    break
            else:
                clusters.append([point])
        built = [
            Level(
                price=quantize_money(sum(group, start=ZERO) / Decimal(len(group))),
                touches=len(group),
                kind=kind,
            )
            for group in clusters
        ]
        return sorted(built, key=lambda level: level.touches, reverse=True)[:limit]

    supports = [
        level
        for level in cluster(_pivots(bars, reach, high=False), "support")
        if level.price < latest
    ]
    resistances = [
        level
        for level in cluster(_pivots(bars, reach, high=True), "resistance")
        if level.price > latest
    ]
    return supports, resistances


# ---------------------------------------------------------------------------
# the summary a page actually shows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TechnicalState:
    """Everything technical about one security, with its gaps admitted.

    Every field is optional because every one of them has a minimum history
    and a security that listed last month genuinely has no 200-day average.
    `None` renders as an em dash; it is never filled in.
    """

    as_of: date
    close: Decimal
    ma20: Decimal | None = None
    ma50: Decimal | None = None
    ma100: Decimal | None = None
    ma200: Decimal | None = None
    rsi14: Decimal | None = None
    macd: Macd | None = None
    atr14: Decimal | None = None
    relative_volume: Decimal | None = None
    gap: Decimal | None = None
    drawdown: Decimal | None = None
    supports: tuple[Level, ...] = ()
    resistances: tuple[Level, ...] = ()
    bars_available: int = 0

    @property
    def trend(self) -> str:
        """A plain-language read of the moving-average stack.

        Deliberately coarse. The stack either lines up or it does not, and
        a finer gradation would imply a precision the inputs do not carry.
        `unknown` is returned rather than a guess when the averages are
        missing.
        """
        if self.ma50 is None or self.ma200 is None:
            return "unknown"
        if self.close > self.ma50 > self.ma200:
            return "uptrend"
        if self.close < self.ma50 < self.ma200:
            return "downtrend"
        return "mixed"

    @property
    def rsi_state(self) -> str:
        if self.rsi14 is None:
            return "unknown"
        if self.rsi14 >= Decimal(70):
            return "overbought"
        if self.rsi14 <= Decimal(30):
            return "oversold"
        return "neutral"

    @property
    def volume_is_abnormal(self) -> bool:
        """Twice the twenty-day average, which is the conventional bar.

        Stated as a fact about volume, not a signal: abnormal volume means
        people traded, not that they were right.
        """
        return self.relative_volume is not None and self.relative_volume >= Decimal(2)


def technical_state(bars: Sequence[Bar]) -> TechnicalState | None:
    """Compute everything at once from one pass over the series."""
    if not bars:
        return None
    require_ascending(bars)
    values = closes(bars)

    return TechnicalState(
        as_of=bars[-1].on,
        close=bars[-1].close,
        ma20=sma(values, 20),
        ma50=sma(values, 50),
        ma100=sma(values, 100),
        ma200=sma(values, 200),
        rsi14=rsi(values),
        macd=macd(values),
        atr14=atr(bars),
        relative_volume=relative_volume(bars),
        gap=gap_percent(bars),
        drawdown=drawdown(values),
        supports=tuple(levels(bars)[0]),
        resistances=tuple(levels(bars)[1]),
        bars_available=len(bars),
    )
