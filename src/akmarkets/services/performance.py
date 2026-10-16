"""Performance application service: assembles inputs, calculates nothing.

Loads a ledger and the price history it needs, hands both to
`akmarkets.domain.returns`, and returns the result. Every figure here was
computed by code that can be tested against a hand-worked reference with no
database and no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from akmarkets.domain.returns import (
    BenchmarkComparison,
    Contribution,
    PeriodReturn,
    ValuePoint,
    attribution,
    compare_to_benchmark,
    period_return,
    value_series,
)
from akmarkets.domain.sessions import last_completed_session, sessions_in_range
from akmarkets.providers.cache import PriceCache
from akmarkets.services import portfolio as portfolio_service

# The periods offered everywhere in the UI. `None` means "since the first
# transaction", which is the only one whose start is not a fixed offset.
PERIODS: dict[str, int | None] = {
    "1d": 1,
    "1w": 7,
    "1m": 30,
    "3m": 91,
    "ytd": 0,  # resolved against 1 January
    "1y": 365,
    "all": None,
}


@dataclass(frozen=True)
class PerformanceReport:
    portfolio_id: str
    period: str
    start: date
    end: date
    series: list[ValuePoint]
    summary: PeriodReturn | None
    contributions: list[Contribution]
    benchmark: BenchmarkComparison
    # Symbols the portfolio holds but could not be priced. Named rather than
    # counted, because "3 positions unpriced" is not actionable and
    # "XMPL unpriced" is.
    unpriced: list[str]

    @property
    def is_complete(self) -> bool:
        return not self.unpriced and (self.summary is not None and not self.summary.incomplete)


def resolve_window(period: str, first_transaction: date | None, today: date) -> tuple[date, date]:
    """Turn a period name into concrete dates.

    The end is the last completed session rather than today: valuing a
    portfolio against a partial session shows a number that will still
    change, which reads as a loss or gain that has not happened yet.
    """
    end = min(last_completed_session(), today)
    if period == "all":
        return (first_transaction or end, end)
    if period == "ytd":
        return (date(end.year, 1, 1), end)
    days = PERIODS.get(period)
    if days is None:
        days = 30
    return (end - timedelta(days=days), end)


def _history_for(
    cache: PriceCache, symbols_by_instrument: dict[str, str]
) -> dict[str, dict[date, Decimal]]:
    """Closes per instrument id, keyed the way the domain expects.

    The domain reasons about instrument ids; the cache is keyed by ticker.
    Translating here keeps the ticker out of the calculation layer, where it
    would eventually be used as an identity and merge two companies.
    """
    return {
        instrument_id: cache.closes(symbol)
        for instrument_id, symbol in symbols_by_instrument.items()
    }


def performance(
    session: Session,
    cache: PriceCache,
    portfolio_id: str,
    period: str = "1m",
    today: date | None = None,
) -> PerformanceReport:
    """Everything the dashboard needs about one portfolio over one window."""
    folio = portfolio_service.get_portfolio(session, portfolio_id)
    transactions = portfolio_service.list_transactions(session, portfolio_id)
    now = today or date.today()
    first = min((t.occurred_on for t in transactions), default=None)
    start, end = resolve_window(period, first, now)

    instrument_ids = sorted({t.instrument_id for t in transactions if t.instrument_id})
    instruments = portfolio_service.list_instruments(session, instrument_ids)
    symbols = {
        instrument_id: instrument.symbol
        for instrument_id, instrument in instruments.items()
    }
    history = _history_for(cache, symbols)

    # Valuation points: every session in the window, capped so a five-year
    # "all" window does not build a thousand-point series for a sparkline.
    days = sessions_in_range(start, end) or [start, end]
    if len(days) > 260:
        step = len(days) // 260 + 1
        days = [*days[::step], days[-1]]
    if days[0] != start:
        days = [start, *days]

    series = value_series(
        portfolio_id=portfolio_id,
        transactions=transactions,
        price_history=history,
        dates=days,
        method=folio.cost_basis_method,
    )
    summary = period_return(series)
    contributions = attribution(
        portfolio_id, transactions, history, start, end, method=folio.cost_basis_method
    )

    # Priced explicitly. Calling portfolio_state without prices makes every
    # open position "unpriced", so the report would claim it was incomplete
    # on a day when every price was present -- and a page that always says
    # its total is unavailable trains the reader to ignore the warning.
    as_of_prices = {
        instrument_id: closes[max(day for day in closes if day <= end)]
        for instrument_id, closes in history.items()
        if any(day <= end for day in closes)
    }
    state = portfolio_service.portfolio_state(
        session, portfolio_id, as_of=end, prices=as_of_prices
    )
    unpriced = sorted(
        symbols.get(instrument_id, instrument_id)
        for instrument_id in state.unpriced_instrument_ids
    )

    benchmark = compare_to_benchmark(
        summary.time_weighted if summary else None,
        cache.closes(folio.benchmark_symbol),
        start,
        end,
        folio.benchmark_symbol,
    )

    return PerformanceReport(
        portfolio_id=portfolio_id,
        period=period,
        start=start,
        end=end,
        series=series,
        summary=summary,
        contributions=contributions,
        benchmark=benchmark,
        unpriced=unpriced,
    )


def portfolio_symbols(session: Session, portfolio_id: str) -> list[str]:
    """Every ticker a portfolio touches, for a price refresh."""
    transactions = portfolio_service.list_transactions(session, portfolio_id)
    instrument_ids = sorted({t.instrument_id for t in transactions if t.instrument_id})
    instruments = portfolio_service.list_instruments(session, instrument_ids)
    folio = portfolio_service.get_portfolio(session, portfolio_id)
    symbols = {instrument.symbol for instrument in instruments.values()}
    # The benchmark is needed to render any comparison, so it is refreshed
    # alongside the holdings rather than being noticed as missing later.
    symbols.add(folio.benchmark_symbol)
    return sorted(symbols)
