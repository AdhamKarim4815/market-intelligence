"""News application service: collect, score, and fill the inbox.

The domain does all the judging; this layer decides what to fetch, persists
what came back, and turns scored events into alerts for the portfolios they
actually matter to.

The one piece of real logic here is **suppression**. An event that already
produced an alert must not produce another one tomorrow because it was
mentioned again — an inbox that repeats itself is an inbox nobody opens, and
that is a worse failure than missing something, because it destroys the
value of everything else in it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.db.base import new_id, utc_now
from akmarkets.db.models import (
    AlertRow,
    NewsArticleRow,
    NewsEventRow,
    PortfolioRow,
    WatchlistEntryRow,
)
from akmarkets.domain.news import (
    Article,
    ScoredEvent,
    Severity,
    classify_source,
    pipeline,
)
from akmarkets.providers.news import GoogleNewsProvider
from akmarkets.services import market_data, performance
from akmarkets.services import portfolio as portfolio_service

# Below this an event is recorded but never becomes an alert. INFO items are
# still readable on the news page -- they are simply not pushed at anyone.
ALERT_FLOOR = Severity.WATCH

_SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.WATCH: 1,
    Severity.IMPORTANT: 2,
    Severity.CRITICAL: 3,
}

# How long an event stays suppressed after alerting. Long enough that the
# tail of coverage does not re-alert; short enough that a genuine
# development in the same story a week later still reaches the inbox.
SUPPRESS_FOR = timedelta(days=5)


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part for part in raw.split(",") if part)


def store_articles(session: Session, articles: Sequence[Article]) -> int:
    """Persist articles we have not seen before.

    Returns how many were new. Existing ids are skipped rather than
    updated: an article's text does not change, and re-writing it would
    reset a `retrieved_at` that provider health reads.

    "Already seen" has to include this batch, not just the database. One story
    is returned for every symbol it mentions, so a feed covering AAPL, GOOGL
    and MSFT hands back the same analyst piece three times with one id -- and
    inserting it twice in a single flush is a UNIQUE violation that takes the
    whole refresh down. With four symbols that collision was rare enough never
    to happen; at twelve it is certain, which is why widening the research
    universe broke news collection instantly.
    """
    if not articles:
        return 0
    ids = [article.article_id for article in articles]
    known = set(
        session.scalars(
            select(NewsArticleRow.article_id).where(NewsArticleRow.article_id.in_(ids))
        ).all()
    )
    now = utc_now()
    fresh = 0
    for article in articles:
        if article.article_id in known:
            continue
        # Before the insert, not after: the guard above is what makes a repeat
        # within this same batch a skip rather than a duplicate row.
        known.add(article.article_id)
        session.add(
            NewsArticleRow(
                article_id=article.article_id,
                fingerprint=article.fingerprint,
                title=article.title,
                url=article.url,
                source=article.source,
                source_class=classify_source(article.source, article.url).name.lower(),
                published_at=article.published_at,
                retrieved_at=now,
                summary=article.summary,
                symbols=",".join(article.symbols),
            )
        )
        fresh += 1
    session.flush()
    return fresh


def recent_articles(session: Session, days: int = 14) -> list[Article]:
    """Everything collected recently, as domain records.

    Re-reading from storage rather than scoring only what just arrived is
    what lets deduplication and clustering span runs. An article seen
    yesterday must not become a new event tonight because the process
    restarted.
    """
    cutoff = utc_now() - timedelta(days=days)
    rows = session.scalars(
        select(NewsArticleRow)
        .where(NewsArticleRow.published_at >= cutoff)
        .order_by(NewsArticleRow.published_at)
    ).all()
    return [
        Article(
            article_id=row.article_id,
            title=row.title,
            url=row.url,
            source=row.source,
            published_at=(
                row.published_at
                if row.published_at.tzinfo
                else row.published_at.replace(tzinfo=UTC)
            ),
            summary=row.summary,
            symbols=_split(row.symbols),
        )
        for row in rows
    ]


def store_events(session: Session, scored: Sequence[ScoredEvent]) -> int:
    """Upsert the clusters, keeping their scoring alongside."""
    now = utc_now()
    for item in scored:
        event = item.event
        row = session.get(NewsEventRow, event.event_id)
        if row is None:
            row = NewsEventRow(event_id=event.event_id, first_seen=event.first_seen)
            session.add(row)
        row.key_title = event.key_title
        row.symbols = ",".join(event.symbols)
        row.last_seen = event.last_seen
        row.article_count = event.size
        row.source_count = len(event.sources)
        row.best_source_class = item.source_class.name.lower()
        row.confirmation = item.confirmation.value
        row.dimensions = ",".join(d.value for d in item.materiality.dimensions)
        row.sentiment_label = (
            item.sentiment.label if item.sentiment.is_meaningful else "no-signal"
        )
        row.updated_at = now

        for article in event.articles:
            article_row = session.get(NewsArticleRow, article.article_id)
            if article_row is not None:
                article_row.event_id = event.event_id
    session.flush()
    return len(scored)


def mark_duplicates(session: Session, duplicates: dict[str, str]) -> None:
    """Record what was collapsed, without deleting it."""
    for dropped, kept in duplicates.items():
        row = session.get(NewsArticleRow, dropped)
        if row is not None:
            row.duplicate_of = kept
    session.flush()


def _recently_alerted(session: Session, portfolio_id: str) -> set[str]:
    cutoff = utc_now() - SUPPRESS_FOR
    rows = session.scalars(
        select(AlertRow.event_id).where(
            AlertRow.portfolio_id == portfolio_id,
            AlertRow.created_at >= cutoff,
            AlertRow.event_id.is_not(None),
        )
    ).all()
    return {event_id for event_id in rows if event_id}


def raise_alerts(
    session: Session, portfolio_id: str, scored: Sequence[ScoredEvent]
) -> list[AlertRow]:
    """Turn scored events into inbox items for one portfolio.

    Two gates. Below `ALERT_FLOOR` nothing is raised — the event is still
    readable, it is simply not pushed. And an event that already alerted
    recently is suppressed, because an inbox that repeats itself is an
    inbox nobody opens, which quietly destroys the value of everything else
    in it.

    A muted event stays muted: the user has said this one does not matter,
    and overriding that would teach them the mute button does not work.
    """
    already = _recently_alerted(session, portfolio_id)
    muted = {
        event_id
        for event_id in session.scalars(
            select(AlertRow.event_id).where(
                AlertRow.portfolio_id == portfolio_id, AlertRow.muted.is_(True)
            )
        ).all()
        if event_id
    }

    raised: list[AlertRow] = []
    now = utc_now()
    for item in scored:
        if _SEVERITY_ORDER[item.severity] < _SEVERITY_ORDER[ALERT_FLOOR]:
            continue
        if item.event.event_id in already or item.event.event_id in muted:
            continue

        alert = AlertRow(
            alert_id=new_id(),
            portfolio_id=portfolio_id,
            event_id=item.event.event_id,
            severity=item.severity.value,
            title=item.event.key_title,
            body=(
                f"{item.event.size} article(s) from {len(item.event.sources)} source(s). "
                f"{item.confirmation.value.replace('_', ' ')}; "
                f"sentiment {item.sentiment.label}."
            ),
            symbols=",".join(item.event.symbols),
            score=str(item.attention.score),
            explanation=item.attention.explain(),
            created_at=now,
        )
        session.add(alert)
        raised.append(alert)
        already.add(item.event.event_id)

    session.flush()
    return raised


def inbox(
    session: Session,
    portfolio_id: str,
    include_read: bool = False,
    include_archived: bool = False,
    limit: int = 100,
) -> list[AlertRow]:
    query = select(AlertRow).where(AlertRow.portfolio_id == portfolio_id)
    if not include_archived:
        query = query.where(AlertRow.archived_at.is_(None))
    if not include_read:
        query = query.where(AlertRow.read_at.is_(None))
    query = query.order_by(AlertRow.created_at.desc()).limit(limit)
    return list(session.scalars(query).all())


def unread_counts(session: Session, portfolio_id: str) -> dict[str, int]:
    """How many unread items at each severity.

    The dashboard leads with this, so it is computed rather than derived
    from a rendered list — the page must be able to say "1 critical, 2
    important" without loading every alert.
    """
    counts = dict.fromkeys((s.value for s in Severity), 0)
    for alert in inbox(session, portfolio_id, limit=500):
        counts[alert.severity] = counts.get(alert.severity, 0) + 1
    return counts


def set_alert_state(
    session: Session,
    alert_id: str,
    read: bool | None = None,
    archived: bool | None = None,
    muted: bool | None = None,
) -> AlertRow | None:
    alert = session.get(AlertRow, alert_id)
    if alert is None:
        return None
    now = utc_now()
    if read is not None:
        alert.read_at = now if read else None
    if archived is not None:
        alert.archived_at = now if archived else None
    if muted is not None:
        alert.muted = muted
        # Muting implies you have dealt with it; leaving it unread would
        # keep it in a count the user has explicitly dismissed.
        if muted and alert.read_at is None:
            alert.read_at = now
    return alert


# -- watchlists ---------------------------------------------------------


def add_to_watchlist(
    session: Session, portfolio_id: str, symbol: str, tier: str = "C", note: str = ""
) -> WatchlistEntryRow:
    normalized = symbol.strip().upper()
    row = session.get(WatchlistEntryRow, (portfolio_id, normalized))
    if row is None:
        row = WatchlistEntryRow(
            portfolio_id=portfolio_id, symbol=normalized, created_at=utc_now()
        )
        session.add(row)
    row.tier = tier
    if note:
        row.note = note
    session.flush()
    return row


def watchlist(session: Session, portfolio_id: str) -> list[WatchlistEntryRow]:
    return list(
        session.scalars(
            select(WatchlistEntryRow)
            .where(WatchlistEntryRow.portfolio_id == portfolio_id)
            .order_by(WatchlistEntryRow.tier, WatchlistEntryRow.symbol)
        ).all()
    )


def remove_from_watchlist(session: Session, portfolio_id: str, symbol: str) -> None:
    row = session.get(WatchlistEntryRow, (portfolio_id, symbol.strip().upper()))
    if row is not None:
        session.delete(row)


def monitored_symbols(session: Session, portfolio_id: str) -> list[str]:
    """What news is collected for: things traded, plus things watched.

    Deliberately *not* `performance.portfolio_symbols`, which adds the
    benchmark. The benchmark is needed to price a comparison, not to be
    reported on — collecting headlines about SPY for a portfolio that holds
    nothing is a request per run that nobody will ever read.

    Watchlist entries are included because something you are considering is
    exactly when news matters most; by the time you own it, you have
    already decided.
    """
    transactions = portfolio_service.list_transactions(session, portfolio_id)
    instrument_ids = sorted({t.instrument_id for t in transactions if t.instrument_id})
    traded = {
        instrument.symbol
        for instrument in portfolio_service.list_instruments(
            session, instrument_ids
        ).values()
    }
    watched = {row.symbol for row in watchlist(session, portfolio_id)}
    return sorted(traded | watched)


# -- weighting ----------------------------------------------------------


def portfolio_weights(
    session: Session, settings: Settings, portfolio_id: str
) -> dict[str, Decimal]:
    """Each symbol's share of the portfolio, for attention scoring.

    A watched but unheld symbol gets a small non-zero weight rather than
    zero: zero would multiply its attention to nothing and it would never
    surface, which defeats the purpose of watching it. The floor is
    deliberately low so a holding always outranks a watch.
    """
    cache = market_data.build_price_cache(settings)
    symbols = performance.portfolio_symbols(session, portfolio_id)
    prices, _ = market_data.closing_prices(cache, symbols)

    transactions = portfolio_service.list_transactions(session, portfolio_id)
    instrument_ids = sorted({t.instrument_id for t in transactions if t.instrument_id})
    instruments = portfolio_service.list_instruments(session, instrument_ids)
    by_instrument = {
        instrument_id: prices[instrument.symbol]
        for instrument_id, instrument in instruments.items()
        if instrument.symbol in prices
    }

    state = portfolio_service.portfolio_state(
        session, portfolio_id, prices=by_instrument
    )
    total = state.positions_value or Decimal(0)

    weights: dict[str, Decimal] = {}
    for position in state.open_positions:
        instrument = instruments.get(position.instrument_id)
        value = position.market_value
        if instrument is None or value is None or total <= 0:
            continue
        weights[instrument.symbol] = value / total

    for row in watchlist(session, portfolio_id):
        weights.setdefault(row.symbol, WATCHLIST_WEIGHT[row.tier])
    return weights


# A watched symbol's stand-in weight, by monitoring tier. Low enough that a
# real holding always outranks a watch of the same news, high enough that a
# critical development on a priority watch still reaches the inbox.
WATCHLIST_WEIGHT: dict[str, Decimal] = {
    "A": Decimal("0.30"),
    "B": Decimal("0.15"),
    "C": Decimal("0.07"),
    "D": Decimal("0.03"),
}


def refresh_news(
    session: Session,
    settings: Settings,
    portfolio_id: str | None = None,
    run_id: str = "",
    provider: Any = None,
) -> dict[str, Any]:
    """Collect, score and alert for one portfolio or all of them."""
    feed = provider or GoogleNewsProvider(
        timeout=settings.news.request_timeout_seconds
    )
    portfolios = (
        [session.get(PortfolioRow, portfolio_id)]
        if portfolio_id
        else list(
            session.scalars(
                select(PortfolioRow).where(PortfolioRow.is_archived.is_(False))
            ).all()
        )
    )
    portfolios = [folio for folio in portfolios if folio is not None]

    collected = 0
    alerted = 0
    degraded = False

    for folio in portfolios:
        symbols = monitored_symbols(session, folio.portfolio_id)
        if not symbols:
            continue
        result = feed.fetch_for_symbols(
            symbols,
            days=settings.news.lookback_days,
            limit_per_symbol=settings.news.max_items_per_feed,
        )
        market_data.record_provider_result(
            session, provider=feed.name, category="news",
            state=result.state, message=result.message,
        )
        if not result.value:
            degraded = True
            continue
        if result.state.value != "healthy":
            degraded = True

        collected += store_articles(session, result.value)

        articles = recent_articles(session, days=settings.news.lookback_days)
        weights = portfolio_weights(session, settings, folio.portfolio_id)
        scored, duplicates = pipeline(articles, weights=weights)

        mark_duplicates(session, duplicates)
        store_events(session, scored)
        alerted += len(raise_alerts(session, folio.portfolio_id, scored))

    return {
        "items": collected,
        "degraded": degraded,
        "detail": f"{collected} new article(s), {alerted} alert(s)",
    }


def now() -> datetime:
    return datetime.now(UTC)
