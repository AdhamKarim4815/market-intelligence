"""Risk application service: aligns series, then hands them to the domain.

The one piece of real work here is **alignment**. Two price series with
different trading holidays, or one that listed later than the other, produce
a correlation that compares Tuesday with Wednesday — and the resulting
number looks perfectly reasonable. Everything is intersected on date before
any statistic is computed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.domain.money import ZERO
from akmarkets.domain.risk import (
    STANDARD_SCENARIOS,
    Concentration,
    HiddenCluster,
    Scenario,
    StressResult,
    beta,
    concentration,
    group_exposure,
    hidden_correlations,
    returns,
    sharpe,
    sortino,
    stress_test,
    volatility,
)
from akmarkets.providers.cache import PriceCache
from akmarkets.services import market_data, news
from akmarkets.services import portfolio as portfolio_service


@dataclass(frozen=True)
class RiskReport:
    portfolio_id: str
    as_of: date
    weights: dict[str, Decimal]
    concentration: Concentration | None
    sector_exposure: dict[str, Decimal]
    betas: dict[str, Decimal]
    portfolio_beta: Decimal | None
    volatility: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    clusters: list[HiddenCluster]
    scenarios: list[StressResult]
    observations: int
    # Symbols excluded from the statistics for want of history. Named, so
    # a beta computed over half a portfolio does not read as a beta for the
    # whole one.
    insufficient_history: tuple[str, ...] = ()


def aligned_returns(
    cache: PriceCache, symbols: list[str]
) -> tuple[dict[str, list[Decimal]], list[date], list[str]]:
    """Return series for every symbol, aligned on a common set of dates.

    Intersected rather than padded. Filling a missing session with the
    previous close invents a zero return, which drags every correlation
    toward zero and every volatility down — quietly making the portfolio
    look safer than it is.
    """
    closes = {symbol: cache.closes(symbol) for symbol in symbols}
    usable = {symbol: series for symbol, series in closes.items() if len(series) > 2}
    if not usable:
        return {}, [], list(symbols)

    common = set.intersection(*(set(series) for series in usable.values()))
    dates = sorted(common)
    if len(dates) < 3:
        return {}, [], list(symbols)

    series_by_symbol = {
        symbol: returns([closes[symbol][day] for day in dates]) for symbol in usable
    }
    excluded = [symbol for symbol in symbols if symbol not in usable]
    return series_by_symbol, dates, excluded


def risk_report(
    session: Session,
    settings: Settings,
    portfolio_id: str,
    scenarios: tuple[Scenario, ...] = STANDARD_SCENARIOS,
) -> RiskReport:
    """Everything the risk page shows, with its gaps named."""
    cache = market_data.build_price_cache(settings)
    weights = {
        symbol: weight
        for symbol, weight in news.portfolio_weights(session, settings, portfolio_id).items()
        if weight > ZERO
    }
    folio = portfolio_service.get_portfolio(session, portfolio_id)
    benchmark = folio.benchmark_symbol

    symbols = sorted(weights)
    series, dates, excluded = aligned_returns(cache, [*symbols, benchmark])
    market = series.get(benchmark, [])

    betas: dict[str, Decimal] = {}
    for symbol in symbols:
        value = beta(series.get(symbol, []), market) if market else None
        if value is not None:
            betas[symbol] = value

    # The portfolio's beta is the weighted average of the betas we could
    # measure, renormalised over exactly those holdings. Treating an
    # unmeasurable holding as beta 1 would invent a sensitivity.
    measured_weight = sum((weights[symbol] for symbol in betas), start=ZERO)
    portfolio_beta = (
        sum((betas[symbol] * weights[symbol] for symbol in betas), start=ZERO)
        / measured_weight
        if measured_weight > ZERO
        else None
    )

    portfolio_returns = _portfolio_returns(series, weights)
    sectors = _sectors(session, symbols)

    stress = [
        stress_test(
            scenario,
            weights=weights,
            betas={f"{symbol}:market": value for symbol, value in betas.items()},
        )
        for scenario in scenarios
    ]

    return RiskReport(
        portfolio_id=portfolio_id,
        as_of=dates[-1] if dates else date.today(),
        weights=weights,
        concentration=concentration(weights),
        sector_exposure=group_exposure(weights, sectors),
        betas=betas,
        portfolio_beta=portfolio_beta,
        volatility=_volatility_of(portfolio_returns),
        sharpe=sharpe(portfolio_returns) if portfolio_returns else None,
        sortino=sortino(portfolio_returns) if portfolio_returns else None,
        clusters=hidden_correlations(
            {symbol: series[symbol] for symbol in symbols if symbol in series}, weights
        ),
        scenarios=stress,
        observations=len(portfolio_returns),
        insufficient_history=tuple(sorted(excluded)),
    )


def _portfolio_returns(
    series: dict[str, list[Decimal]], weights: dict[str, Decimal]
) -> list[Decimal]:
    """A synthetic return series for the portfolio at current weights.

    Current weights rather than historical ones, and the limitation is
    real: it answers "how would today's portfolio have behaved" rather than
    "how did my portfolio behave". The second needs a full historical
    reconstruction and is what the strategy lab does; this one is for
    characterising the portfolio as it stands.
    """
    usable = {symbol: values for symbol, values in series.items() if symbol in weights}
    if not usable:
        return []
    total = sum((weights[symbol] for symbol in usable), start=ZERO)
    if total <= ZERO:
        return []
    length = min(len(values) for values in usable.values())
    return [
        sum(
            (weights[symbol] / total * usable[symbol][index] for symbol in usable),
            start=ZERO,
        )
        for index in range(length)
    ]


def _volatility_of(period_returns: list[Decimal]) -> Decimal | None:
    if not period_returns:
        return None
    # `volatility` takes a price series; these are already returns, so the
    # deviation is taken directly rather than differencing twice.
    from akmarkets.domain.risk import MIN_OBSERVATIONS, stdev

    if len(period_returns) < MIN_OBSERVATIONS:
        return None
    return stdev(period_returns)


def _sectors(session: Session, symbols: list[str]) -> dict[str, str]:
    from sqlalchemy import select

    from akmarkets.db.models import InstrumentRow

    rows = session.scalars(
        select(InstrumentRow).where(InstrumentRow.symbol.in_(symbols))
    ).all()
    return {row.symbol: row.sector for row in rows if row.sector}


__all__ = ["RiskReport", "aligned_returns", "risk_report", "volatility"]
