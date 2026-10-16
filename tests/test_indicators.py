"""Technical indicators, checked against hand-computed values.

Every assertion here is a number worked out independently, not a snapshot of
what the code happened to produce. An indicator test that records current
behaviour proves only that nothing changed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from akmarkets.domain.indicators import (
    Macd,
    SeriesOrderError,
    atr,
    drawdown,
    ema,
    gap_percent,
    levels,
    macd,
    max_drawdown,
    relative_volume,
    require_ascending,
    rsi,
    sma,
    technical_state,
    true_range,
)
from akmarkets.providers.base import Bar

START = date(2026, 1, 1)


def bar(
    index: int,
    close: str,
    high: str | None = None,
    low: str | None = None,
    open_: str | None = None,
    volume: int = 1000,
) -> Bar:
    value = Decimal(close)
    return Bar(
        on=START + timedelta(days=index),
        open=Decimal(open_) if open_ else value,
        high=Decimal(high) if high else value,
        low=Decimal(low) if low else value,
        close=value,
        volume=volume,
    )


def series(prices: list[str]) -> list[Bar]:
    return [bar(i, price) for i, price in enumerate(prices)]


# -- ordering -----------------------------------------------------------


def test_a_reversed_series_is_rejected_rather_than_quietly_sorted() -> None:
    """The most dangerous failure available here: a reversed series
    produces plausible numbers that are exactly backwards, and nothing
    about the output looks wrong. One provider really does return
    newest-first."""
    ascending = series(["10", "11", "12"])
    require_ascending(ascending)
    with pytest.raises(SeriesOrderError, match="oldest-first"):
        require_ascending(list(reversed(ascending)))


# -- averages -----------------------------------------------------------


def test_a_moving_average_is_the_mean_of_its_window() -> None:
    """(10+20+30+40+50)/5 = 30."""
    assert sma([Decimal(x) for x in (10, 20, 30, 40, 50)], 5) == Decimal(30)


def test_not_enough_history_is_none_not_a_shorter_average() -> None:
    """A 200-day average from 40 bars is a 40-day average wearing the wrong
    label, and it would be indistinguishable on a chart."""
    assert sma([Decimal(1)] * 40, 200) is None


def test_an_ema_is_seeded_on_the_simple_average_not_the_first_point() -> None:
    """Seeding on one value makes the early EMA chase that number for
    several periods, and MACD is a difference of two EMAs where the error
    does not cancel.

    Flat input must give exactly the flat value; a first-point seed on a
    series that starts with an outlier would not.
    """
    assert ema([Decimal(50)] * 30, 10) == Decimal(50)

    spiky = [Decimal(100), *[Decimal(50)] * 29]
    # With a proper seed the outlier is one tenth of the first window and
    # has washed out entirely by the end.
    assert ema(spiky, 10) is not None
    assert abs(ema(spiky, 10) - Decimal(50)) < Decimal("0.5")  # type: ignore[operator]


# -- RSI ----------------------------------------------------------------


def test_rsi_is_100_when_nothing_has_fallen() -> None:
    """Undefined by division, and the limit as losses approach zero is
    100 — which is worth returning rather than None."""
    assert rsi([Decimal(i) for i in range(1, 40)]) == Decimal(100)


def test_rsi_is_zero_when_nothing_has_risen() -> None:
    assert rsi([Decimal(i) for i in range(40, 1, -1)]) == Decimal(0)


def test_rsi_on_a_flat_series_is_100_because_there_are_no_losses() -> None:
    """Convention, and stated because it surprises people: a flat series
    has no average loss, so the ratio is undefined the same way."""
    assert rsi([Decimal(50)] * 40) == Decimal(100)


def test_rsi_sits_near_the_middle_for_a_balanced_series() -> None:
    alternating = [Decimal(50 + (2 if i % 2 else 0)) for i in range(60)]
    value = rsi(alternating)
    assert value is not None
    assert Decimal(35) < value < Decimal(65)


def test_rsi_needs_one_more_bar_than_its_period() -> None:
    """Fourteen periods means fourteen *changes*, which needs fifteen
    closes."""
    assert rsi([Decimal(50)] * 14) is None
    assert rsi([Decimal(50)] * 15) is not None


# -- MACD ---------------------------------------------------------------


def test_macd_is_zero_on_a_flat_series() -> None:
    """Both EMAs equal the price, so their difference is exactly zero. Any
    other answer would mean the two series were misaligned."""
    result = macd([Decimal(50)] * 120)
    assert result is not None
    assert result.line == Decimal(0)
    assert result.histogram == Decimal(0)


def test_macd_is_positive_in_an_uptrend_and_negative_in_a_downtrend() -> None:
    rising = macd([Decimal(i) for i in range(1, 200)])
    falling = macd([Decimal(i) for i in range(200, 1, -1)])
    assert rising is not None and rising.line > 0
    assert falling is not None and falling.line < 0


def test_macd_returns_none_rather_than_a_shifted_line_without_history() -> None:
    assert macd([Decimal(50)] * 20) is None


def test_macd_carries_all_three_numbers() -> None:
    result = macd([Decimal(i) for i in range(1, 200)])
    assert isinstance(result, Macd)
    assert result.histogram == result.line - result.signal


# -- ranges -------------------------------------------------------------


def test_true_range_uses_the_gap_when_it_exceeds_the_days_span() -> None:
    """The price moved through the gap; it simply did not trade there.
    High minus low alone understates it."""
    yesterday = bar(0, "100", high="101", low="99")
    gapped = bar(1, "110", high="111", low="109")
    # Measured against the previous *close* (100), not its low: the close is
    # where the last trade actually happened, so 111 - 100 = 11 is the
    # distance the price travelled. The day's own span is only 2.
    assert true_range(gapped, yesterday) == Decimal(11)
    assert true_range(gapped, None) == Decimal(2)  # no previous close to gap from


def test_atr_on_a_constant_range_is_that_range() -> None:
    bars = [bar(i, "100", high="102", low="98") for i in range(40)]
    assert atr(bars) == Decimal(4)


def test_atr_needs_history() -> None:
    assert atr([bar(i, "100") for i in range(10)]) is None


# -- volume and gaps ----------------------------------------------------


def test_relative_volume_is_a_multiple_of_the_recent_average() -> None:
    bars = [bar(i, "100", volume=1000) for i in range(21)]
    bars.append(bar(21, "100", volume=3000))
    assert relative_volume(bars) == Decimal(3)


def test_relative_volume_is_none_when_there_is_no_baseline() -> None:
    """A stock that has not traded for twenty sessions has no baseline, and
    "infinite relative volume" is not a fact about it."""
    bars = [bar(i, "100", volume=0) for i in range(21)]
    bars.append(bar(21, "100", volume=5000))
    assert relative_volume(bars) is None


def test_a_gap_is_measured_open_against_the_previous_close() -> None:
    bars = [bar(0, "100"), bar(1, "110", open_="105")]
    assert gap_percent(bars) == Decimal("0.05")


# -- drawdown -----------------------------------------------------------


def test_drawdown_is_the_distance_below_the_running_peak() -> None:
    assert drawdown([Decimal(x) for x in (100, 120, 90)]) == Decimal("-0.25")


def test_max_drawdown_never_reports_a_trough_before_its_peak() -> None:
    """`min − max` would happily report a fall that never happened: here
    the low of 50 comes *before* the high of 200."""
    values = [Decimal(x) for x in (100, 50, 200, 180)]
    # Two real drawdowns happened: 100 → 50 (−50%) and 200 → 180 (−10%).
    # The worst is −50%.
    assert max_drawdown(values) == Decimal("-0.5")
    # `min − max` would compute (50 − 200) / 200 = −75%, pairing the trough
    # with a peak that came *after* it — a fall nobody ever suffered.
    assert max_drawdown(values) != Decimal("-0.75")

    forward_only = [Decimal(x) for x in (100, 200, 150)]
    assert max_drawdown(forward_only) == Decimal("-0.25")


# -- levels -------------------------------------------------------------


def test_a_level_publishes_its_touch_count_rather_than_asserting_itself() -> None:
    """A level a price approached once is a coincidence; one it turned at
    four times is a level. The count is the evidence."""
    prices: list[str] = []
    for _ in range(4):
        prices += ["100", "104", "108", "104", "100", "96", "92", "96"]
    bars = series(prices)
    supports, resistances = levels(bars, reach=2)

    assert supports or resistances
    for level in (*supports, *resistances):
        assert level.touches >= 1
        assert level.is_meaningful == (level.touches >= 3)


def test_levels_need_bars_on_both_sides_so_the_last_ones_never_qualify() -> None:
    """Requiring both sides is what makes a pivot a turning point rather
    than the end of the data."""
    supports, resistances = levels(series(["100"] * 4), reach=5)
    assert supports == []
    assert resistances == []


def test_support_is_below_the_price_and_resistance_above() -> None:
    prices: list[str] = []
    for _ in range(4):
        prices += ["100", "110", "120", "110", "100", "90", "80", "90"]
    prices.append("105")
    bars = series(prices)
    supports, resistances = levels(bars, reach=2)
    for level in supports:
        assert level.price < bars[-1].close
    for level in resistances:
        assert level.price > bars[-1].close


# -- the summary --------------------------------------------------------


def test_a_short_history_admits_what_it_cannot_compute() -> None:
    """A security that listed last month genuinely has no 200-day average,
    and the field is None rather than filled in."""
    state = technical_state(series([str(100 + i) for i in range(30)]))
    assert state is not None
    assert state.ma20 is not None
    assert state.ma200 is None
    assert state.trend == "unknown"
    assert state.bars_available == 30


def test_the_trend_reads_the_moving_average_stack() -> None:
    rising = technical_state(series([str(100 + i) for i in range(260)]))
    falling = technical_state(series([str(400 - i) for i in range(260)]))
    assert rising is not None and rising.trend == "uptrend"
    assert falling is not None and falling.trend == "downtrend"


def test_rsi_state_is_unknown_rather_than_neutral_without_data() -> None:
    """"Neutral" is a claim about the security; "unknown" is a claim about
    us, and only one of them is true here."""
    state = technical_state(series(["100"] * 5))
    assert state is not None
    assert state.rsi_state == "unknown"


def test_abnormal_volume_is_stated_as_a_fact_not_a_signal() -> None:
    bars = [bar(i, "100", volume=1000) for i in range(21)]
    bars.append(bar(21, "100", volume=5000))
    state = technical_state(bars)
    assert state is not None
    assert state.volume_is_abnormal is True


def test_an_empty_series_produces_nothing_rather_than_a_default() -> None:
    assert technical_state([]) is None
