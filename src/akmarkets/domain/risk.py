"""Portfolio risk, with its statistics stated rather than implied.

Every function here returns `None` rather than a number when the sample is
too small to support one. That is the whole discipline of the module: a beta
from twelve observations is not a rough beta, it is noise with a Greek
letter on it, and it will be read as a measurement.

The minimum sample sizes below are stated, not fitted, and each is the point
where the statistic stops being nonsense rather than the point where it
becomes reliable — which is much further out and depends on the data.

**Nothing here is annualised by default.** An annualised Sharpe from three
months of daily returns implies an accuracy the sample cannot support.
Where annualisation is offered it is explicit and labelled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from akmarkets.domain.money import ZERO

# Below this a statistic is not computed at all. Sixty daily observations is
# roughly a quarter — the point at which a variance estimate stops being
# dominated by whichever week happened to be in the sample.
MIN_OBSERVATIONS = 60

# Correlation needs fewer points to be meaningful than a variance ratio,
# but not many fewer.
MIN_CORRELATION_OBSERVATIONS = 40

# Trading days in a year, for the explicit annualisation helpers.
TRADING_DAYS = 252


def returns(values: Sequence[Decimal]) -> list[Decimal]:
    """Simple period returns from a price series.

    A period with a zero or missing base is skipped rather than treated as
    a 100% move — a stock that was untradeable produces no return, not an
    enormous one.
    """
    return [
        (later - earlier) / earlier
        for earlier, later in pairwise(values)
        if earlier != ZERO
    ]


def mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, start=ZERO) / Decimal(len(values))


def variance(values: Sequence[Decimal]) -> Decimal | None:
    """Sample variance, with Bessel's correction.

    `n − 1` rather than `n`: these are samples of an unknown process, not
    the whole population, and dividing by `n` biases the estimate downward —
    which in a risk figure means understating risk.
    """
    if len(values) < 2:
        return None
    average = mean(values)
    if average is None:
        return None
    total = sum(((value - average) ** 2 for value in values), start=ZERO)
    return total / Decimal(len(values) - 1)


def _sqrt(value: Decimal) -> Decimal:
    """Square root staying in Decimal.

    Newton's method rather than `math.sqrt`, so a risk figure does not
    silently change with the platform's floating point — the same series
    must produce the same number on every machine and in every run.
    """
    if value <= ZERO:
        return ZERO
    guess = value
    for _ in range(60):
        better = (guess + value / guess) / Decimal(2)
        if abs(better - guess) < Decimal("1E-20"):
            return better
        guess = better
    return guess


def stdev(values: Sequence[Decimal]) -> Decimal | None:
    result = variance(values)
    return None if result is None else _sqrt(result)


def volatility(
    series: Sequence[Decimal], annualise: bool = False
) -> Decimal | None:
    """Standard deviation of period returns.

    `annualise` is opt-in and does the conventional √252 scaling. It is not
    the default because an annualised figure from a short sample implies an
    accuracy the sample cannot support.
    """
    period_returns = returns(series)
    if len(period_returns) < MIN_OBSERVATIONS:
        return None
    deviation = stdev(period_returns)
    if deviation is None:
        return None
    return deviation * _sqrt(Decimal(TRADING_DAYS)) if annualise else deviation


def covariance(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = mean(left), mean(right)
    if left_mean is None or right_mean is None:
        return None
    total = sum(
        ((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)),
        start=ZERO,
    )
    return total / Decimal(len(left) - 1)


def correlation(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal | None:
    """Pearson correlation of two return series, or None.

    Both series must be the same length and aligned by date *before* they
    get here. Correlating two series with different holidays silently
    compares Tuesday with Wednesday, and the resulting number looks
    perfectly reasonable.
    """
    if len(left) != len(right) or len(left) < MIN_CORRELATION_OBSERVATIONS:
        return None
    left_dev, right_dev = stdev(left), stdev(right)
    shared = covariance(left, right)
    if not left_dev or not right_dev or shared is None:
        return None
    return shared / (left_dev * right_dev)


def beta(
    asset_returns: Sequence[Decimal], market_returns: Sequence[Decimal]
) -> Decimal | None:
    """Sensitivity to the market, or None when the sample cannot support it.

    A beta from twelve observations is noise with a Greek letter on it, and
    it will be read as a measurement.
    """
    if len(asset_returns) != len(market_returns) or len(asset_returns) < MIN_OBSERVATIONS:
        return None
    market_variance = variance(market_returns)
    shared = covariance(asset_returns, market_returns)
    if not market_variance or shared is None:
        return None
    return shared / market_variance


def sharpe(
    period_returns: Sequence[Decimal],
    risk_free_rate: Decimal = ZERO,
    annualise: bool = False,
) -> Decimal | None:
    """Excess return per unit of total volatility.

    The risk-free rate is a *period* rate, matching the returns, and
    defaults to zero rather than to a guess. Silently assuming a rate is how
    a Sharpe ratio becomes uncomparable with every other Sharpe ratio.
    """
    if len(period_returns) < MIN_OBSERVATIONS:
        return None
    average = mean(period_returns)
    deviation = stdev(period_returns)
    if average is None or not deviation:
        return None
    ratio = (average - risk_free_rate) / deviation
    return ratio * _sqrt(Decimal(TRADING_DAYS)) if annualise else ratio


def sortino(
    period_returns: Sequence[Decimal],
    risk_free_rate: Decimal = ZERO,
    annualise: bool = False,
) -> Decimal | None:
    """Excess return per unit of *downside* volatility.

    Downside deviation is computed over the whole sample, not just the
    negative observations — dividing by the count of losses instead makes a
    portfolio with two bad days look riskier than one with twenty, which is
    backwards.
    """
    if len(period_returns) < MIN_OBSERVATIONS:
        return None
    average = mean(period_returns)
    if average is None:
        return None
    downside = [
        (value - risk_free_rate) ** 2
        for value in period_returns
        if value < risk_free_rate
    ]
    if not downside:
        # No period fell below the target. The ratio is undefined rather
        # than infinite, and returning a huge number would rank this
        # portfolio first in any sort.
        return None
    deviation = _sqrt(sum(downside, start=ZERO) / Decimal(len(period_returns)))
    if deviation == ZERO:
        return None
    ratio = (average - risk_free_rate) / deviation
    return ratio * _sqrt(Decimal(TRADING_DAYS)) if annualise else ratio


# ---------------------------------------------------------------------------
# concentration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Concentration:
    """How much of the portfolio sits in how few places."""

    largest: tuple[str, Decimal] | None
    top_three: Decimal
    herfindahl: Decimal
    effective_positions: Decimal | None

    @property
    def is_concentrated(self) -> bool:
        """Fewer than five effective positions.

        A count, not a judgement about whether that is wrong — concentration
        is a choice, and the product's job is to make it visible rather than
        to disapprove of it.
        """
        return (
            self.effective_positions is not None
            and self.effective_positions < Decimal(5)
        )


def concentration(weights: dict[str, Decimal]) -> Concentration | None:
    """Concentration measures over portfolio weights.

    The Herfindahl index — the sum of squared weights — is reported
    alongside its reciprocal, the *effective number of positions*, because
    the raw index means nothing to most readers and "you effectively hold
    2.3 things" means a great deal.
    """
    if not weights:
        return None
    ordered = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    index = sum((weight**2 for weight in weights.values()), start=ZERO)
    return Concentration(
        largest=ordered[0] if ordered else None,
        top_three=sum((weight for _, weight in ordered[:3]), start=ZERO),
        herfindahl=index,
        effective_positions=(Decimal(1) / index) if index > ZERO else None,
    )


def group_exposure(
    weights: dict[str, Decimal], groups: dict[str, str]
) -> dict[str, Decimal]:
    """Weight by sector, country, or any other grouping.

    A symbol with no group lands in `unknown` rather than being dropped:
    silently excluding it would make the exposures sum to less than one and
    understate whichever group it belonged to.
    """
    totals: dict[str, Decimal] = {}
    for symbol, weight in weights.items():
        key = groups.get(symbol) or "unknown"
        totals[key] = totals.get(key, ZERO) + weight
    return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


# ---------------------------------------------------------------------------
# hidden correlation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HiddenCluster:
    """Positions that look different and move together."""

    members: tuple[str, ...]
    average_correlation: Decimal
    combined_weight: Decimal

    @property
    def is_significant(self) -> bool:
        return self.combined_weight >= Decimal("0.2")


def hidden_correlations(
    return_series: dict[str, Sequence[Decimal]],
    weights: dict[str, Decimal],
    threshold: Decimal = Decimal("0.7"),
) -> list[HiddenCluster]:
    """Find groups of holdings that move together.

    A portfolio of ten stocks can be one concentrated macro bet, and this is
    what makes that visible. Clustering is single-linkage on the correlation
    matrix — deliberately simple, because the finding is qualitative ("these
    four move together") and a more sophisticated method would imply a
    precision the sample sizes do not support.
    """
    symbols = [
        symbol
        for symbol in return_series
        if len(return_series[symbol]) >= MIN_CORRELATION_OBSERVATIONS
    ]
    if len(symbols) < 2:
        return []

    pairs: dict[tuple[str, str], Decimal] = {}
    for index, left in enumerate(symbols):
        for right in symbols[index + 1 :]:
            value = correlation(return_series[left], return_series[right])
            if value is not None and value >= threshold:
                pairs[(left, right)] = value

    # Single-linkage: anything correlated with a cluster member joins it.
    clusters: list[set[str]] = []
    for (left, right), _ in pairs.items():
        for cluster in clusters:
            if left in cluster or right in cluster:
                cluster.update({left, right})
                break
        else:
            clusters.append({left, right})

    results: list[HiddenCluster] = []
    for cluster in clusters:
        members = tuple(sorted(cluster))
        values = [
            value
            for (left, right), value in pairs.items()
            if left in cluster and right in cluster
        ]
        if not values:
            continue
        results.append(
            HiddenCluster(
                members=members,
                average_correlation=sum(values, start=ZERO) / Decimal(len(values)),
                combined_weight=sum(
                    (weights.get(member, ZERO) for member in members), start=ZERO
                ),
            )
        )
    return sorted(results, key=lambda item: item.combined_weight, reverse=True)


# ---------------------------------------------------------------------------
# stress testing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """A hypothetical shock, and what it is assumed to do.

    `assumptions` is part of the result, not documentation. A stress test
    whose assumptions are not shown alongside its number is a forecast
    wearing a lab coat.
    """

    name: str
    shocks: dict[str, Decimal]
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class StressResult:
    scenario: str
    portfolio_change: Decimal
    by_symbol: dict[str, Decimal]
    assumptions: tuple[str, ...]
    unmodelled: tuple[str, ...]

    @property
    def caveat(self) -> str:
        return (
            "Modelled outcome under the stated assumptions, not a forecast. "
            "Betas are estimates from a finite sample and relationships that "
            "held historically change, most reliably during the events a "
            "stress test is meant to describe."
        )


def stress_test(
    scenario: Scenario,
    weights: dict[str, Decimal],
    betas: dict[str, Decimal],
) -> StressResult:
    """Apply a shock through each holding's sensitivity to it.

    A holding with no estimated beta to the shocked factor is reported in
    `unmodelled` rather than assumed to have a beta of zero or one. Assuming
    zero understates the impact and assuming one overstates it; naming the
    gap is the only honest option.
    """
    impacts: dict[str, Decimal] = {}
    unmodelled: list[str] = []

    for symbol, weight in weights.items():
        symbol_impact = ZERO
        modelled = False
        for factor, shock in scenario.shocks.items():
            sensitivity = betas.get(f"{symbol}:{factor}")
            if sensitivity is None:
                continue
            symbol_impact += weight * sensitivity * shock
            modelled = True
        if modelled:
            impacts[symbol] = symbol_impact
        else:
            unmodelled.append(symbol)

    return StressResult(
        scenario=scenario.name,
        portfolio_change=sum(impacts.values(), start=ZERO),
        by_symbol=dict(sorted(impacts.items(), key=lambda item: item[1])),
        assumptions=scenario.assumptions,
        unmodelled=tuple(sorted(unmodelled)),
    )


STANDARD_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="Market falls 10%",
        shocks={"market": Decimal("-0.10")},
        assumptions=(
            "each holding moves by its estimated beta to the benchmark",
            "beta is measured over the trailing sample and is assumed stable",
            "no correlation breakdown, which is exactly what happens in a real fall",
        ),
    ),
    Scenario(
        name="Market rises 10%",
        shocks={"market": Decimal("0.10")},
        assumptions=("each holding moves by its estimated beta to the benchmark",),
    ),
)
