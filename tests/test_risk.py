"""Risk statistics, and the sample sizes they refuse to be computed below."""

from __future__ import annotations

from decimal import Decimal

from akmarkets.domain.risk import (
    MIN_OBSERVATIONS,
    Scenario,
    beta,
    concentration,
    correlation,
    group_exposure,
    hidden_correlations,
    returns,
    sharpe,
    sortino,
    stdev,
    stress_test,
    variance,
    volatility,
)


def series(values: list[float]) -> list[Decimal]:
    return [Decimal(str(value)) for value in values]


def steady(n: int, step: str = "0.01") -> list[Decimal]:
    """A return series long enough to be computed on."""
    return [Decimal(step) if index % 2 else Decimal(step) * -1 for index in range(n)]


# -- refusals -----------------------------------------------------------


def test_a_statistic_is_none_below_its_minimum_sample() -> None:
    """A beta from twelve observations is noise with a Greek letter on it,
    and it will be read as a measurement."""
    short = steady(12)
    assert beta(short, short) is None
    assert sharpe(short) is None
    assert sortino(short) is None
    assert volatility([Decimal(100 + i) for i in range(12)]) is None


def test_the_same_statistics_compute_at_the_minimum() -> None:
    long = steady(MIN_OBSERVATIONS)
    assert beta(long, long) is not None
    assert sharpe(long) is not None


def test_correlation_refuses_mismatched_series() -> None:
    """Correlating two series with different holidays silently compares
    Tuesday with Wednesday, and the number looks perfectly reasonable."""
    assert correlation(steady(60), steady(59)) is None


# -- the arithmetic -----------------------------------------------------


def test_variance_uses_bessels_correction() -> None:
    """Dividing by n biases the estimate downward, which in a risk figure
    means understating risk."""
    values = series([2, 4, 4, 4, 5, 5, 7, 9])
    # Population variance is 4; the sample estimate with n−1 is 32/7.
    assert variance(values) == Decimal(32) / Decimal(7)


def test_standard_deviation_is_the_root_of_variance() -> None:
    values = series([2, 4, 4, 4, 5, 5, 7, 9])
    deviation = stdev(values)
    assert deviation is not None
    assert abs(deviation**2 - Decimal(32) / Decimal(7)) < Decimal("1E-15")


def test_square_root_stays_in_decimal_and_is_reproducible() -> None:
    """A risk figure must not change with the platform's floating point:
    the same series has to produce the same number everywhere."""
    from akmarkets.domain.risk import _sqrt

    assert isinstance(_sqrt(Decimal(2)), Decimal)
    assert abs(_sqrt(Decimal(2)) ** 2 - Decimal(2)) < Decimal("1E-20")
    assert _sqrt(Decimal(0)) == Decimal(0)
    assert _sqrt(Decimal(-1)) == Decimal(0)


def test_returns_skip_a_period_with_a_zero_base() -> None:
    """A stock that was untradeable produces no return, not an enormous
    one."""
    assert returns(series([100, 0, 50])) == [Decimal(-1)]


def test_beta_against_itself_is_one() -> None:
    market = [Decimal(str((index % 7) - 3)) / Decimal(100) for index in range(80)]
    result = beta(market, market)
    assert result is not None
    assert abs(result - Decimal(1)) < Decimal("1E-20")


def test_beta_of_a_doubled_series_is_two() -> None:
    market = [Decimal(str((index % 7) - 3)) / Decimal(100) for index in range(80)]
    levered = [value * 2 for value in market]
    result = beta(levered, market)
    assert result is not None
    assert abs(result - Decimal(2)) < Decimal("1E-18")


def test_correlation_is_one_for_identical_and_minus_one_for_inverse() -> None:
    left = [Decimal(str((index % 5) - 2)) / Decimal(100) for index in range(60)]
    assert abs((correlation(left, left) or Decimal(0)) - Decimal(1)) < Decimal("1E-18")
    inverse = [-value for value in left]
    assert abs((correlation(left, inverse) or Decimal(0)) + Decimal(1)) < Decimal("1E-18")


# -- ratios -------------------------------------------------------------


def test_the_risk_free_rate_defaults_to_zero_rather_than_a_guess() -> None:
    """Silently assuming a rate is how a Sharpe ratio becomes uncomparable
    with every other Sharpe ratio."""
    values = steady(80)
    assert sharpe(values) == sharpe(values, risk_free_rate=Decimal(0))


def test_annualisation_is_opt_in() -> None:
    """An annualised figure from a short sample implies an accuracy the
    sample cannot support."""
    values = [Decimal(str((index % 9) - 4)) / Decimal(1000) for index in range(120)]
    plain = sharpe(values)
    annual = sharpe(values, annualise=True)
    assert plain is not None and annual is not None
    assert abs(annual) > abs(plain)


def test_sortino_is_undefined_rather_than_infinite_without_losses() -> None:
    """Returning a huge number would rank this portfolio first in any
    sort."""
    all_gains = [Decimal("0.01")] * 80
    assert sortino(all_gains) is None


def test_sortino_divides_by_the_whole_sample_not_just_the_losses() -> None:
    """Dividing by the count of losses makes a portfolio with two bad days
    look riskier than one with twenty, which is backwards."""
    two_bad = [Decimal("0.01")] * 78 + [Decimal("-0.05")] * 2
    twenty_bad = [Decimal("0.01")] * 60 + [Decimal("-0.05")] * 20
    first, second = sortino(two_bad), sortino(twenty_bad)
    assert first is not None and second is not None
    assert first > second


def test_volatility_annualises_by_the_root_of_the_trading_year() -> None:
    prices = [Decimal(100) + Decimal(str(index % 5)) for index in range(120)]
    plain = volatility(prices)
    annual = volatility(prices, annualise=True)
    assert plain is not None and annual is not None
    assert abs(annual / plain - Decimal(252).sqrt()) < Decimal("1E-10")


# -- concentration ------------------------------------------------------


def test_effective_positions_says_what_the_index_means() -> None:
    """"You effectively hold 2 things" means a great deal; a Herfindahl of
    0.5 means nothing to most readers."""
    result = concentration({"A": Decimal("0.5"), "B": Decimal("0.5")})
    assert result is not None
    assert result.herfindahl == Decimal("0.5")
    assert result.effective_positions == Decimal(2)
    assert result.is_concentrated is True


def test_a_spread_portfolio_is_not_flagged() -> None:
    even = {f"S{index}": Decimal("0.1") for index in range(10)}
    result = concentration(even)
    assert result is not None
    assert result.effective_positions is not None
    assert abs(result.effective_positions - Decimal(10)) < Decimal("1E-20")
    assert result.is_concentrated is False


def test_concentration_reports_the_largest_and_the_top_three() -> None:
    result = concentration(
        {"A": Decimal("0.4"), "B": Decimal("0.3"), "C": Decimal("0.2"), "D": Decimal("0.1")}
    )
    assert result is not None
    assert result.largest == ("A", Decimal("0.4"))
    assert result.top_three == Decimal("0.9")


def test_an_ungrouped_holding_lands_in_unknown_rather_than_vanishing() -> None:
    """Dropping it would make the exposures sum to less than one and
    understate whichever group it belonged to."""
    exposure = group_exposure(
        {"A": Decimal("0.5"), "B": Decimal("0.5")}, {"A": "Technology"}
    )
    assert exposure == {"Technology": Decimal("0.5"), "unknown": Decimal("0.5")}
    assert sum(exposure.values()) == Decimal(1)


def test_no_weights_produce_no_concentration_figure() -> None:
    assert concentration({}) is None


# -- hidden correlation -------------------------------------------------


def test_positions_that_look_different_and_move_together_are_surfaced() -> None:
    """A portfolio of ten stocks can be one concentrated macro bet."""
    base = [Decimal(str((index % 11) - 5)) / Decimal(100) for index in range(80)]
    noise = [Decimal(str((index % 3) - 1)) / Decimal(10000) for index in range(80)]
    lithium_one = base
    lithium_two = [a + b for a, b in zip(base, noise, strict=True)]
    unrelated = [Decimal(str((index % 7) - 3)) / Decimal(100) for index in range(80)]

    clusters = hidden_correlations(
        {"XMPL": lithium_one, "ALB": lithium_two, "GOOGL": unrelated},
        {"XMPL": Decimal("0.2"), "ALB": Decimal("0.2"), "GOOGL": Decimal("0.6")},
    )
    assert clusters
    assert set(clusters[0].members) == {"XMPL", "ALB"}
    assert clusters[0].combined_weight == Decimal("0.4")
    assert clusters[0].is_significant is True


def test_a_short_series_is_excluded_from_clustering() -> None:
    short = [Decimal("0.01")] * 10
    assert hidden_correlations({"A": short, "B": short}, {}) == []


# -- stress testing -----------------------------------------------------


def test_a_stress_result_carries_its_assumptions() -> None:
    """A stress test whose assumptions are not shown alongside its number is
    a forecast wearing a lab coat."""
    scenario = Scenario(
        name="Market falls 10%",
        shocks={"market": Decimal("-0.10")},
        assumptions=("each holding moves by its estimated beta",),
    )
    result = stress_test(
        scenario,
        weights={"A": Decimal("0.5"), "B": Decimal("0.5")},
        betas={"A:market": Decimal("1.0"), "B:market": Decimal("2.0")},
    )
    # 0.5×1.0×−0.10 + 0.5×2.0×−0.10 = −0.15
    assert result.portfolio_change == Decimal("-0.15")
    assert result.assumptions
    assert "not a forecast" in result.caveat


def test_a_holding_with_no_beta_is_named_not_assumed() -> None:
    """Assuming zero understates the impact and assuming one overstates it;
    naming the gap is the only honest option."""
    result = stress_test(
        Scenario(name="x", shocks={"market": Decimal("-0.10")}),
        weights={"A": Decimal("0.5"), "B": Decimal("0.5")},
        betas={"A:market": Decimal("1.0")},
    )
    assert result.unmodelled == ("B",)
    assert result.portfolio_change == Decimal("-0.05")


def test_the_caveat_names_correlation_breakdown() -> None:
    """The assumption most likely to fail is the one that fails during
    exactly the events a stress test describes."""
    from akmarkets.domain.risk import STANDARD_SCENARIOS

    fall = STANDARD_SCENARIOS[0]
    assert any("correlation breakdown" in item for item in fall.assumptions)


# -- the service --------------------------------------------------------


def test_series_are_intersected_on_date_not_padded(tmp_path: object) -> None:
    """Filling a missing session with the previous close invents a zero
    return, which drags every correlation toward zero and every volatility
    down — quietly making the portfolio look safer than it is."""
    from datetime import date as _date
    from datetime import timedelta

    from akmarkets.domain.market import Bar
    from akmarkets.providers.cache import PriceCache
    from akmarkets.providers.chain import HistoryChain
    from akmarkets.services.risk import aligned_returns

    class Never:
        name = "never"

        def fetch_history(self, symbol: str, start: object, end: object) -> object:
            raise AssertionError("no fetch expected")

    cache = PriceCache(root=tmp_path / "p", chain=HistoryChain([Never()]))  # type: ignore[operator,list-item]
    start = _date(2026, 1, 5)

    def bars(days: list[int]) -> list[Bar]:
        return [
            Bar(
                on=start + timedelta(days=day),
                open=Decimal(100 + day), high=Decimal(101 + day),
                low=Decimal(99 + day), close=Decimal(100 + day), volume=100,
            )
            for day in days
        ]

    cache.store("A", bars([0, 1, 2, 3, 4]))
    cache.store("B", bars([0, 2, 4]))  # missing two sessions

    series, dates, excluded = aligned_returns(cache, ["A", "B"])
    assert dates == [start, start + timedelta(days=2), start + timedelta(days=4)]
    assert len(series["A"]) == len(series["B"]) == 2
    assert excluded == []


def test_a_symbol_with_no_history_is_excluded_and_named(tmp_path: object) -> None:
    """A beta computed over half a portfolio must not read as a beta for
    the whole one."""
    from akmarkets.providers.cache import PriceCache
    from akmarkets.providers.chain import HistoryChain
    from akmarkets.services.risk import aligned_returns

    class Never:
        name = "never"

        def fetch_history(self, symbol: str, start: object, end: object) -> object:
            raise AssertionError("no fetch expected")

    cache = PriceCache(root=tmp_path / "p2", chain=HistoryChain([Never()]))  # type: ignore[operator,list-item]
    _, _, excluded = aligned_returns(cache, ["NOPE"])
    assert excluded == ["NOPE"]
