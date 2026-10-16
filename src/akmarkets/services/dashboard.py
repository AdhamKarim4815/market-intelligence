"""The overview page's view model. Assembles; calculates nothing.

Every figure here was computed somewhere that can be tested against a
hand-worked reference. This module's only job is to gather them into one
object so the template has no logic in it, and so the page can be tested
without a browser.

Two properties are load-bearing and easy to lose:

**A section that failed is not a section that is empty.** Each part is
gathered independently and a failure is recorded in `degraded` rather than
swallowed. "We could not read the yield curve" and "there is no yield
curve" are different facts, and a table rendered empty for the first reason
is a quiet lie. The same reasoning as `web/routes/health.py`, for the same
reason: the page that would explain an outage must not be its first
casualty.

**Nothing here reaches the network.** `build_price_cache` constructs the
provider chain but performs no I/O, and `closing_prices` reads the local
Parquet cache. A dashboard that fetched on page load would be one provider
outage away from a page that hangs, and one refresh key away from a rate
limit. Prices arrive through the refresh pipeline or not at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.db.models import AlertRow, DataFindingRow, EarningsEventRow, JobRunRow
from akmarkets.domain.records import Portfolio, PortfolioState, Position
from akmarkets.providers.cache import PriceCache
from akmarkets.services import jobs as jobs_service
from akmarkets.services import macro as macro_service
from akmarkets.services import market_data, performance
from akmarkets.services import news as news_service
from akmarkets.services import portfolio as portfolio_service

# How many portfolio cards the page renders before it stops and links to
# `/portfolios`. Set now rather than discovered later: the cost of this page
# is linear in the number of portfolios, and a bound decided in advance is
# one nobody has to debug under load.
MAX_CARDS = 6

# How much rate history to load. Deliberately more than a year: the curve
# table answers "where are rates versus a year ago", and `macro._change`
# correctly refuses to answer a 1-year question from 11 months of data. A
# tighter window would not fail — it would silently render every 1y column
# as an em dash, which looks like missing data rather than a bad query.
RATE_WINDOW_DAYS = 400

@dataclass(frozen=True)
class PortfolioCard:
    """One portfolio, as the overview shows it.

    Holds `state` and `report` rather than copying figures out of them.
    Those objects already expose `total_value`, `unrealized_gain` and the
    rest as properties that know when to return `None`; re-deriving them
    here would be a second place for that judgement to drift.
    """

    portfolio: Portfolio
    state: PortfolioState
    report: performance.PerformanceReport | None
    # instrument_id -> ticker, so the template can label rows without
    # reaching for the database itself.
    symbols: dict[str, str]
    unread: dict[str, int]
    top_alerts: list[AlertRow]
    finding_count: int

    @property
    def unread_total(self) -> int:
        return sum(self.unread.values())

    @property
    def top_positions(self) -> list[Position]:
        """Open positions, largest first, unpriced last.

        Unpriced positions sort last rather than being dropped: a holding
        with no price is still a holding, and hiding it from the summary is
        how a portfolio silently looks smaller than it is.
        """
        return sorted(
            self.state.open_positions,
            key=lambda p: (p.market_value is not None, p.market_value or Decimal(0)),
            reverse=True,
        )


@dataclass(frozen=True)
class Overview:
    """Everything the overview page renders."""

    portfolios: list[PortfolioCard] = field(default_factory=list)
    universe: list[str] = field(default_factory=list)
    rates: list[macro_service.RateSnapshot] = field(default_factory=list)
    earnings: list[EarningsEventRow] = field(default_factory=list)
    provider_health: list[dict[str, object]] = field(default_factory=list)
    open_findings: list[DataFindingRow] = field(default_factory=list)
    recent_runs: list[JobRunRow] = field(default_factory=list)
    # Sections that raised while being gathered. Not the same as a section
    # with nothing in it, and the template must say so differently.
    degraded: tuple[str, ...] = ()
    truncated: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.portfolios


def prices_by_instrument(
    session: Session, portfolio_id: str, prices_by_symbol: dict[str, Decimal]
) -> dict[str, Decimal]:
    """Re-key prices from ticker to instrument id.

    Public because three callers need it and two of them had grown their
    own copy. The ledger keys positions by instrument id; providers key
    everything by ticker; something has to bridge them, once.
    """
    instrument_ids = [
        transaction.instrument_id
        for transaction in portfolio_service.list_transactions(session, portfolio_id)
        if transaction.instrument_id
    ]
    instruments = portfolio_service.list_instruments(session, instrument_ids)
    return {
        instrument_id: prices_by_symbol[instrument.symbol]
        for instrument_id, instrument in instruments.items()
        if instrument.symbol in prices_by_symbol
    }


def _gather[T](name: str, produce: Callable[[], T], fallback: T, degraded: list[str]) -> T:
    """Run one section, recording a failure instead of propagating it.

    Broad by intent. The caller cannot enumerate what a provider table, a
    ledger replay and a yield curve might each raise, and the page is more
    useful with six sections and a named failure than with none at all.
    """
    try:
        return produce()
    except Exception as exc:
        degraded.append(f"{name}: {type(exc).__name__}")
        return fallback


def overview(
    session: Session,
    settings: Settings,
    user_id: str,
    period: str = "1m",
    today: date | None = None,
    alert_limit: int = 3,
    earnings_days: int = 14,
) -> Overview:
    """Assemble the overview for one user."""
    degraded: list[str] = []
    reference = today or date.today()
    no_folios: list[Portfolio] = []
    no_symbols: list[str] = []

    folios = _gather(
        "portfolios",
        lambda: portfolio_service.list_portfolios(session, user_id),
        no_folios,
        degraded,
    )
    visible = folios[:MAX_CARDS]

    cache = market_data.build_price_cache(settings)
    cards: list[PortfolioCard] = []
    for folio in visible:
        card = _gather(
            f"portfolio {folio.name}",
            lambda folio=folio: _card(  # type: ignore[misc]
                session, cache, folio, period, reference, alert_limit
            ),
            None,
            degraded,
        )
        if card is not None:
            cards.append(card)

    universe = _gather(
        "universe",
        lambda: sorted(
            {
                symbol
                for folio in visible
                for symbol in news_service.monitored_symbols(session, folio.portfolio_id)
            }
        ),
        no_symbols,
        degraded,
    )

    no_rates: list[macro_service.RateSnapshot] = []
    no_earnings: list[EarningsEventRow] = []
    no_health: list[dict[str, object]] = []
    no_findings: list[DataFindingRow] = []
    no_runs: list[JobRunRow] = []

    return Overview(
        portfolios=cards,
        universe=universe,
        rates=_gather(
            "rates",
            lambda: macro_service.latest_rates(
                session, since=reference - timedelta(days=RATE_WINDOW_DAYS)
            ),
            no_rates,
            degraded,
        ),
        earnings=_gather(
            "earnings",
            lambda: macro_service.upcoming_earnings(
                session, symbols=universe, within_days=earnings_days, today=reference
            ),
            no_earnings,
            degraded,
        ),
        provider_health=_gather(
            "provider health", lambda: market_data.provider_health(session), no_health, degraded
        ),
        open_findings=_gather(
            "findings", lambda: market_data.open_findings(session, limit=5), no_findings, degraded
        ),
        recent_runs=_gather(
            "jobs", lambda: jobs_service.recent_runs(session, limit=8), no_runs, degraded
        ),
        degraded=tuple(degraded),
        truncated=max(len(folios) - len(visible), 0),
    )


def _card(
    session: Session,
    cache: PriceCache,
    folio: Portfolio,
    period: str,
    reference: date,
    alert_limit: int,
) -> PortfolioCard:
    """One portfolio's figures, all from services that already own them."""
    symbols = performance.portfolio_symbols(session, folio.portfolio_id)
    prices, _ = market_data.closing_prices(cache, symbols)
    by_instrument = prices_by_instrument(session, folio.portfolio_id, prices)

    state = portfolio_service.portfolio_state(
        session, folio.portfolio_id, prices=by_instrument
    )
    report = performance.performance(
        session,
        cache,
        folio.portfolio_id,
        period=period,
        today=reference,
    )

    held = portfolio_service.list_instruments(
        session, [position.instrument_id for position in state.positions]
    )
    return PortfolioCard(
        portfolio=folio,
        state=state,
        report=report,
        symbols={
            instrument_id: instrument.symbol for instrument_id, instrument in held.items()
        },
        unread=news_service.unread_counts(session, folio.portfolio_id),
        top_alerts=list(
            news_service.inbox(session, folio.portfolio_id, limit=alert_limit)
        ),
        finding_count=len(
            portfolio_service.validate_portfolio(session, folio.portfolio_id)
        ),
    )


def universe_of(session: Session, portfolio_ids: Sequence[str]) -> list[str]:
    """Every symbol monitored across the given portfolios."""
    return sorted(
        {
            symbol
            for portfolio_id in portfolio_ids
            for symbol in news_service.monitored_symbols(session, portfolio_id)
        }
    )
