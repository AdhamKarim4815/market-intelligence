"""Market data application service: prices, freshness, provider health.

Assembles the provider chain from configuration, keeps the Parquet cache
current, and records what each provider actually did. The recording is the
part that matters: a provider that quietly stopped answering is worse than
one that fails loudly, because a dashboard built on it keeps showing the
last number it ever saw.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.db.base import new_id, utc_now
from akmarkets.db.models import DataFindingRow, ProviderHealthRow
from akmarkets.domain.sessions import last_completed_session
from akmarkets.providers.base import (
    DataState,
    Disagreement,
    HistoryProvider,
    Quote,
    QuoteProvider,
)
from akmarkets.providers.cache import PriceCache
from akmarkets.providers.chain import HistoryChain, QuoteChain
from akmarkets.providers.prices import (
    NasdaqHistoryProvider,
    NasdaqQuoteProvider,
    YahooHistoryProvider,
    YahooQuoteProvider,
)

# Typed as factories rather than `type[Protocol]`: a Protocol is not
# instantiable as far as the type checker is concerned, and what the
# registry actually holds is 'something that builds one'.
_HISTORY_PROVIDERS: dict[str, Callable[..., HistoryProvider]] = {
    "nasdaq": NasdaqHistoryProvider,
    "yahoo": YahooHistoryProvider,
}
_QUOTE_PROVIDERS: dict[str, Callable[..., QuoteProvider]] = {
    "nasdaq": NasdaqQuoteProvider,
    "yahoo": YahooQuoteProvider,
}


def build_history_chain(settings: Settings) -> HistoryChain:
    """The configured chain, skipping names with no adapter.

    An unknown provider name in configuration is ignored rather than fatal:
    a typo should degrade the chain, not stop the application from starting
    at all. The health page shows which providers are actually in use.
    """
    providers = [
        _HISTORY_PROVIDERS[name](timeout=settings.prices.request_timeout_seconds)
        for name in settings.prices.provider_priority
        if name in _HISTORY_PROVIDERS
    ]
    if not providers:
        providers = [NasdaqHistoryProvider(timeout=settings.prices.request_timeout_seconds)]
    return HistoryChain(providers)


def build_quote_chain(settings: Settings) -> QuoteChain:
    providers = [
        _QUOTE_PROVIDERS[name](timeout=settings.prices.request_timeout_seconds)
        for name in settings.prices.provider_priority
        if name in _QUOTE_PROVIDERS
    ]
    if not providers:
        providers = [NasdaqQuoteProvider(timeout=settings.prices.request_timeout_seconds)]
    return QuoteChain(
        providers,
        disagreement_tolerance_percent=Decimal(
            str(settings.prices.disagreement_tolerance_pct)
        ),
    )


def build_price_cache(settings: Settings, chain: HistoryChain | None = None) -> PriceCache:
    return PriceCache(
        root=settings.project_root / "var" / "prices",
        chain=chain or build_history_chain(settings),
        calendar=settings.prices.exchange_calendar,
        history_days=settings.prices.history_days,
        max_workers=settings.prices.max_workers,
    )


# -- health -------------------------------------------------------------


def record_provider_result(
    session: Session,
    provider: str,
    category: str,
    state: DataState,
    message: str = "",
) -> None:
    """Update one provider's health record.

    `consecutive_failures` is what distinguishes a blip from an outage. A
    single timeout is normal on free endpoints and should not turn a
    dashboard red; five in a row is a provider that has stopped working.
    """
    now = utc_now()
    row = session.get(ProviderHealthRow, (provider, category))
    if row is None:
        # `consecutive_failures` is set here rather than relying on the
        # column default: a SQLAlchemy default is applied by the INSERT, so
        # a freshly constructed row reads `None` until it is flushed, and
        # the increment below would fail on the very first failure a
        # provider ever reports.
        row = ProviderHealthRow(
            provider=provider,
            category=category,
            consecutive_failures=0,
            updated_at=now,
        )
        session.add(row)

    row.last_attempt_at = now
    row.state = state.value
    row.message = message
    if state.is_usable:
        row.last_success_at = now
        row.consecutive_failures = 0
    else:
        row.last_failure_at = now
        row.consecutive_failures += 1
    row.updated_at = now


def provider_health(session: Session) -> list[dict[str, object]]:
    """Every provider's state, with staleness derived rather than stored.

    A record whose last attempt succeeded but whose last success predates
    the most recent completed session is reported `stale`, not `healthy`.
    Trusting the stored state alone is how a feed that stopped updating a
    week ago keeps showing green.
    """
    rows = session.scalars(
        select(ProviderHealthRow).order_by(
            ProviderHealthRow.category, ProviderHealthRow.provider
        )
    ).all()
    settled = last_completed_session()
    report: list[dict[str, object]] = []
    for row in rows:
        state = DataState(row.state)
        if state.is_usable and (
            row.last_success_at is None or row.last_success_at.date() < settled
        ):
            state = DataState.STALE
        report.append(
            {
                "provider": row.provider,
                "category": row.category,
                "state": state.value,
                "message": row.message,
                "last_success_at": row.last_success_at,
                "consecutive_failures": row.consecutive_failures,
            }
        )
    return report


# -- findings -----------------------------------------------------------


def record_finding(
    session: Session,
    code: str,
    message: str,
    severity: str = "warn",
    subject_type: str = "",
    subject_id: str = "",
    run_id: str = "",
) -> str:
    """Record something that does not look right.

    Re-recording an identical open finding updates it rather than adding a
    duplicate, so a problem that persists across fifty runs is one row with
    a first-seen time, not fifty rows nobody reads.
    """
    existing = session.scalar(
        select(DataFindingRow).where(
            DataFindingRow.code == code,
            DataFindingRow.subject_type == subject_type,
            DataFindingRow.subject_id == subject_id,
            DataFindingRow.resolved_at.is_(None),
        )
    )
    if existing is not None:
        existing.message = message
        existing.run_id = run_id
        return existing.finding_id

    finding_id = new_id()
    session.add(
        DataFindingRow(
            finding_id=finding_id,
            code=code,
            severity=severity,
            subject_type=subject_type,
            subject_id=subject_id,
            message=message,
            run_id=run_id,
            detected_at=utc_now(),
        )
    )
    return finding_id


def record_disagreements(
    session: Session, disagreements: Sequence[Disagreement], run_id: str = ""
) -> None:
    """Providers disagreeing about a price is a data problem, not noise."""
    for finding in disagreements:
        values = ", ".join(f"{name}={value}" for name, value in sorted(finding.values.items()))
        record_finding(
            session,
            code="provider_disagreement",
            severity="warn",
            subject_type="symbol",
            subject_id=finding.subject,
            message=(
                f"{finding.metric} differs by {finding.spread_percent:.2f}% "
                f"between providers ({values})"
            ),
            run_id=run_id,
        )


def open_findings(session: Session, limit: int = 100) -> list[DataFindingRow]:
    return list(
        session.scalars(
            select(DataFindingRow)
            .where(DataFindingRow.resolved_at.is_(None))
            .order_by(DataFindingRow.detected_at.desc())
            .limit(limit)
        ).all()
    )


def resolve_findings(session: Session, code: str, subject_id: str) -> int:
    """Close findings that a clean run has disproved.

    Resolved rather than deleted: the history of what was once wrong is
    what makes "has this always been broken?" answerable.
    """
    rows = session.scalars(
        select(DataFindingRow).where(
            DataFindingRow.code == code,
            DataFindingRow.subject_id == subject_id,
            DataFindingRow.resolved_at.is_(None),
        )
    ).all()
    for row in rows:
        row.resolved_at = utc_now()
    return len(rows)


# -- prices -------------------------------------------------------------


def refresh_prices(
    session: Session,
    settings: Settings,
    symbols: Sequence[str],
    cache: PriceCache | None = None,
    force: bool = False,
    run_id: str = "",
) -> dict[str, str]:
    """Bring the cache up to date, recording health and findings as it goes.

    Returns a state per symbol. One symbol failing never fails the batch:
    a delisted ticker in a watchlist must not stop the rest of a portfolio
    being revalued.
    """
    prices = cache or build_price_cache(settings)
    results = prices.ensure(symbols, force=force)

    outcomes: dict[str, str] = {}
    worst = DataState.HEALTHY
    messages: list[str] = []

    for symbol, result in sorted(results.items()):
        outcomes[symbol] = result.state.value
        if result.state is DataState.FAILED:
            worst = DataState.FAILED
            messages.append(f"{symbol}: {result.message[:80]}")
            record_finding(
                session,
                code="price_fetch_failed",
                severity="error",
                subject_type="symbol",
                subject_id=symbol,
                message=result.message,
                run_id=run_id,
            )
        elif result.state is DataState.UNKNOWN:
            if worst is DataState.HEALTHY:
                worst = DataState.DEGRADED
            record_finding(
                session,
                code="price_series_unavailable",
                severity="warn",
                subject_type="symbol",
                subject_id=symbol,
                message=result.message,
                run_id=run_id,
            )
        else:
            resolve_findings(session, "price_fetch_failed", symbol)
            resolve_findings(session, "price_series_unavailable", symbol)

    record_provider_result(
        session,
        provider=prices.chain.name,
        category="history",
        state=worst,
        message="; ".join(messages[:3]),
    )
    return outcomes


def latest_quotes(
    session: Session,
    settings: Settings,
    symbols: Sequence[str],
    cross_check: bool = False,
    run_id: str = "",
) -> dict[str, Quote]:
    """Delayed quotes, with health and any disagreement recorded."""
    chain = build_quote_chain(settings)
    result, disagreements = chain.fetch_quotes(symbols, cross_check=cross_check)
    record_provider_result(
        session,
        provider=chain.name,
        category="quotes",
        state=result.state,
        message=result.message,
    )
    if disagreements:
        record_disagreements(session, disagreements, run_id=run_id)
    return result.value or {}


def closing_prices(
    cache: PriceCache, symbols: Sequence[str], on: date | None = None
) -> tuple[dict[str, Decimal], dict[str, date]]:
    """Closes as of a date, plus the date each one actually came from.

    Returns only what is known. A symbol with no usable price is absent
    from both dictionaries rather than present with a zero, which is what
    lets the portfolio engine mark the total unknown instead of quietly
    understating it.
    """
    as_of = on or last_completed_session()
    prices: dict[str, Decimal] = {}
    dates: dict[str, date] = {}
    for symbol in symbols:
        close = cache.close_on(symbol, as_of)
        if close is None:
            continue
        prices[symbol] = close
        latest = cache.latest_session(symbol)
        dates[symbol] = min(latest, as_of) if latest else as_of
    return prices, dates
