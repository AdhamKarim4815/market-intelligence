"""The Strategy Lab, and the look-ahead it exists to make impossible."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from akmarkets.domain.backtest import (
    MIN_SAMPLE,
    AsOf,
    LookAheadError,
    Signal,
    calibration,
    evaluate,
    forward_returns,
    replay,
    summarise,
)
from akmarkets.domain.market import Bar

START = date(2026, 1, 5)


def bars(prices: list[str]) -> list[Bar]:
    return [
        Bar(
            on=START + timedelta(days=index),
            open=Decimal(price),
            high=Decimal(price),
            low=Decimal(price),
            close=Decimal(price),
            volume=1000,
        )
        for index, price in enumerate(prices)
    ]


# -- look-ahead ---------------------------------------------------------


def test_asking_for_the_future_raises_rather_than_being_filtered() -> None:
    """A silent filter means a caller with a bug gets a plausible answer and
    never learns. The whole point of a replay is that it cannot cheat."""
    cut = AsOf(START)
    cut.check(START)
    cut.check(START - timedelta(days=1))
    with pytest.raises(LookAheadError, match="after the as-of date"):
        cut.check(START + timedelta(days=1))


def test_only_bars_up_to_the_cut_off_are_visible() -> None:
    series = bars(["10", "11", "12", "13"])
    visible = AsOf(START + timedelta(days=1)).bars(series)
    assert [bar.on for bar in visible] == [START, START + timedelta(days=1)]


def test_the_latest_visible_bar_is_never_tomorrows() -> None:
    series = bars(["10", "99"])
    latest = AsOf(START).latest(series)
    assert latest is not None
    assert latest.close == Decimal(10)


def test_a_rule_is_never_handed_the_full_series() -> None:
    """A stronger guarantee than asking it not to look."""
    series = bars([str(10 + index) for index in range(10)])
    seen: list[int] = []

    def rule(symbol: str, visible: list[Bar], cut: AsOf) -> bool:
        seen.append(len(visible))
        # Anything the rule can see is at or before the cut-off.
        assert all(bar.on <= cut.on for bar in visible)
        return False

    replay([START + timedelta(days=n) for n in range(5)], {"X": series}, rule)
    assert seen == [1, 2, 3, 4, 5]


# -- forward returns ----------------------------------------------------


def test_returns_are_measured_from_bars_after_the_signal() -> None:
    """Entry is the close on the signal date — the earliest price a decision
    made that day could realistically have achieved."""
    series = bars(["100", "110", "120", "130", "140", "150"])
    entry, results = forward_returns(series, START, horizons=(1, 3))
    assert entry == Decimal(100)
    assert results[1] == Decimal("0.1")
    assert results[3] == Decimal("0.3")


def test_a_horizon_past_the_end_produces_nothing_rather_than_a_short_one() -> None:
    """A "120-day return" measured over 40 days is not a rough answer; it is
    a different measurement wearing the wrong label."""
    series = bars(["100", "110", "120"])
    _, results = forward_returns(series, START, horizons=(1, 20))
    assert 1 in results
    assert 20 not in results


def test_a_signal_on_the_last_bar_has_no_outcome() -> None:
    series = bars(["100", "110"])
    _, results = forward_returns(series, START + timedelta(days=1), horizons=(1,))
    assert results == {}


def test_a_signal_on_a_date_with_no_bar_produces_nothing() -> None:
    entry, results = forward_returns(bars(["100"]), date(2020, 1, 1))
    assert entry is None
    assert results == {}


# -- excess -------------------------------------------------------------


def test_excess_is_measured_against_the_benchmark() -> None:
    """A raw return over a rising market is not evidence a signal worked."""
    stock = bars(["100", "110"])
    market = bars(["100", "108"])
    outcomes = evaluate(
        [Signal("X", START)], {"X": stock}, benchmark=market, horizons=(1,)
    )
    assert outcomes[0].returns[1] == Decimal("0.1")
    assert outcomes[0].excess(1) == Decimal("0.02")


def test_without_a_benchmark_excess_falls_back_to_the_raw_return() -> None:
    outcomes = evaluate([Signal("X", START)], {"X": bars(["100", "110"])}, horizons=(1,))
    assert outcomes[0].excess(1) == Decimal("0.1")


# -- summarising --------------------------------------------------------


def test_a_small_sample_is_reported_and_marked_not_hidden() -> None:
    """Eleven observations prove nothing, and the reader has to be able to
    see that."""
    outcomes = evaluate(
        [Signal("X", START)], {"X": bars(["100", "110", "120"])}, horizons=(1,)
    )
    result = summarise(outcomes, horizons=(1,))[0]
    assert result.count == 1
    assert result.is_meaningful is False
    assert "too few to mean anything" in result.describe()


def test_median_is_reported_because_one_outcome_can_carry_a_mean() -> None:
    """With samples this small that is the normal case, not the
    exception."""
    series = {
        "A": bars(["100", "101"]),
        "B": bars(["100", "101"]),
        "C": bars(["100", "300"]),
    }
    outcomes = evaluate(
        [Signal(symbol, START) for symbol in series], series, horizons=(1,)
    )
    result = summarise(outcomes, horizons=(1,))[0]
    assert result.mean_excess is not None and result.median_excess is not None
    assert result.mean_excess > result.median_excess
    assert result.median_excess == Decimal("0.01")


def test_the_beat_rate_says_how_often_rather_than_how_much() -> None:
    series = {
        "A": bars(["100", "110"]),
        "B": bars(["100", "90"]),
        "C": bars(["100", "105"]),
    }
    result = summarise(
        evaluate([Signal(s, START) for s in series], series, horizons=(1,)),
        horizons=(1,),
    )[0]
    assert result.beat_rate == Decimal(2) / Decimal(3)


def test_no_outcomes_produce_a_zero_row_rather_than_a_missing_one() -> None:
    result = summarise([], horizons=(1,))[0]
    assert result.count == 0
    assert result.mean_excess is None
    assert "no outcomes" in result.describe()


def test_best_and_worst_are_carried_so_the_spread_is_visible() -> None:
    series = {"A": bars(["100", "150"]), "B": bars(["100", "50"])}
    result = summarise(
        evaluate([Signal(s, START) for s in series], series, horizons=(1,)),
        horizons=(1,),
    )[0]
    assert result.best == Decimal("0.5")
    assert result.worst == Decimal("-0.5")


# -- replay -------------------------------------------------------------


def test_a_replay_fires_only_where_the_rule_says_so() -> None:
    series = bars(["100", "101", "150", "151"])

    def jumped(symbol: str, visible: list[Bar], cut: AsOf) -> bool:
        if len(visible) < 2:
            return False
        return (visible[-1].close - visible[-2].close) / visible[-2].close > Decimal(
            "0.1"
        )

    steps = replay([START + timedelta(days=n) for n in range(4)], {"X": series}, jumped)
    fired = [step.on for step in steps if step.signals]
    assert fired == [START + timedelta(days=2)]


# -- calibration --------------------------------------------------------


def test_calibration_compares_stated_confidence_with_what_happened() -> None:
    """If the platform says 80% a hundred times, roughly eighty should
    happen. This is the only thing that makes a confidence number mean
    anything."""
    predictions = [(Decimal("0.9"), True)] * 8 + [(Decimal("0.9"), False)] * 2
    top = calibration(predictions, buckets=5)[-1]
    assert top.count == 10
    assert top.predicted == Decimal("0.9")
    assert top.observed == Decimal("0.8")
    assert top.gap == Decimal("-0.1")


def test_an_overconfident_system_shows_a_negative_gap() -> None:
    predictions = [(Decimal("0.9"), True)] * 2 + [(Decimal("0.9"), False)] * 8
    top = calibration(predictions, buckets=5)[-1]
    assert top.gap is not None and top.gap < 0


def test_a_thin_bucket_is_marked_rather_than_dropped() -> None:
    """A calibration chart that silently drops its thin buckets looks far
    better calibrated than the system actually is."""
    buckets = calibration([(Decimal("0.9"), True)], buckets=5)
    populated = [bucket for bucket in buckets if bucket.count]
    assert len(populated) == 1
    assert populated[0].is_meaningful is False
    # The empty buckets are still present, with no observed rate at all.
    assert any(bucket.count == 0 and bucket.observed is None for bucket in buckets)


def test_the_minimum_sample_is_stated_not_hidden() -> None:
    assert MIN_SAMPLE == 30


def test_no_predictions_produce_no_buckets() -> None:
    assert calibration([]) == []
