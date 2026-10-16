"""Market Brain service: snapshots, theses, decisions, and what changed.

Assembles the inputs the pure engines need and persists what they produce.
The one design decision worth stating: **snapshots are how "what changed"
knows what "before" was**. Without a stored prior state the engine can only
compare against whatever happens to be in the cache, which silently becomes
"compared against the last time anyone looked".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.db.base import new_id, utc_now
from akmarkets.db.models import (
    AlertRow,
    DecisionRow,
    IntelligenceSnapshotRow,
    NewsEventRow,
    PositionDriverRow,
    ThesisRow,
)
from akmarkets.domain.brain import (
    ChangeSet,
    Driver,
    MoveAnalysis,
    PositionDna,
    explain_move,
    thesis_warnings,
    what_changed,
)
from akmarkets.domain.news import Severity
from akmarkets.services import market_data, security

# Bumped whenever the meaning of a stored snapshot changes. A score computed
# under a different version is not comparable with one computed under this,
# and a backtest that ignores the difference measures two things at once.
METHODOLOGY = "v1"


@dataclass(frozen=True)
class BrainView:
    symbol: str
    changes: ChangeSet
    move: MoveAnalysis | None
    dna: PositionDna
    warnings: list[str]


# -- theses and drivers -------------------------------------------------


def set_thesis(
    session: Session,
    portfolio_id: str,
    symbol: str,
    thesis: str = "",
    target: str = "",
    horizon: str = "",
    risks: str = "",
    invalidation: str = "",
    opened_on: date | None = None,
) -> ThesisRow:
    normalized = symbol.strip().upper()
    row = session.scalar(
        select(ThesisRow).where(
            ThesisRow.portfolio_id == portfolio_id, ThesisRow.symbol == normalized
        )
    )
    now = utc_now()
    if row is None:
        row = ThesisRow(
            thesis_id=new_id(),
            portfolio_id=portfolio_id,
            symbol=normalized,
            created_at=now,
        )
        session.add(row)
    row.thesis = thesis
    row.target = target
    row.horizon = horizon
    row.risks = risks
    row.invalidation = invalidation
    row.opened_on = opened_on
    row.updated_at = now
    session.flush()
    return row


def get_thesis(session: Session, portfolio_id: str, symbol: str) -> ThesisRow | None:
    return session.scalar(
        select(ThesisRow).where(
            ThesisRow.portfolio_id == portfolio_id,
            ThesisRow.symbol == symbol.strip().upper(),
        )
    )


def set_drivers(
    session: Session, portfolio_id: str, symbol: str, drivers: Sequence[tuple[str, str, str]]
) -> int:
    """Replace the driver set for a position.

    Replaced rather than merged: a driver list is a statement about what
    this position is a bet on, and half-updating it would leave a claim
    nobody made.
    """
    normalized = symbol.strip().upper()
    for row in session.scalars(
        select(PositionDriverRow).where(
            PositionDriverRow.portfolio_id == portfolio_id,
            PositionDriverRow.symbol == normalized,
        )
    ).all():
        session.delete(row)
    session.flush()

    now = utc_now()
    for name, direction, note in drivers:
        session.add(
            PositionDriverRow(
                portfolio_id=portfolio_id,
                symbol=normalized,
                driver=name.strip(),
                direction=direction,
                note=note,
                created_at=now,
            )
        )
    session.flush()
    return len(drivers)


def position_dna(session: Session, portfolio_id: str, symbol: str) -> PositionDna:
    normalized = symbol.strip().upper()
    rows = session.scalars(
        select(PositionDriverRow).where(
            PositionDriverRow.portfolio_id == portfolio_id,
            PositionDriverRow.symbol == normalized,
        )
    ).all()
    thesis = get_thesis(session, portfolio_id, normalized)
    return PositionDna(
        symbol=normalized,
        drivers=tuple(
            Driver(name=row.driver, direction=row.direction, note=row.note) for row in rows
        ),
        thesis=thesis.thesis if thesis else "",
        invalidation=tuple(thesis.invalidation_criteria) if thesis else (),
    )


# -- decisions ----------------------------------------------------------


def record_decision(
    session: Session,
    portfolio_id: str,
    action: str,
    decided_on: date,
    symbol: str = "",
    reason: str = "",
    expected: str = "",
    horizon: str = "",
) -> DecisionRow:
    row = DecisionRow(
        decision_id=new_id(),
        portfolio_id=portfolio_id,
        symbol=symbol.strip().upper(),
        action=action,
        decided_on=decided_on,
        reason=reason,
        expected=expected,
        horizon=horizon,
        created_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row


def decisions(session: Session, portfolio_id: str, limit: int = 50) -> list[DecisionRow]:
    return list(
        session.scalars(
            select(DecisionRow)
            .where(DecisionRow.portfolio_id == portfolio_id)
            .order_by(DecisionRow.decided_on.desc())
            .limit(limit)
        ).all()
    )


def review_decision(
    session: Session, decision_id: str, note: str, reviewed_on: date | None = None
) -> DecisionRow | None:
    row = session.get(DecisionRow, decision_id)
    if row is None:
        return None
    row.review_note = note
    row.reviewed_on = reviewed_on or date.today()
    return row


# -- snapshots ----------------------------------------------------------


def take_snapshot(
    session: Session,
    settings: Settings,
    symbol: str,
    run_id: str = "",
    as_of: date | None = None,
) -> IntelligenceSnapshotRow | None:
    """Record what the platform believes about a security today.

    One row per (symbol, date), updated on re-run, so a second refresh in
    one day corrects rather than duplicates.
    """
    view = security.security_view(session, settings, symbol)
    if view is None:
        return None
    when = as_of or view.as_of

    row = session.scalar(
        select(IntelligenceSnapshotRow).where(
            IntelligenceSnapshotRow.symbol == view.instrument.symbol,
            IntelligenceSnapshotRow.as_of == when,
        )
    )
    if row is None:
        row = IntelligenceSnapshotRow(
            snapshot_id=new_id(),
            symbol=view.instrument.symbol,
            as_of=when,
            created_at=utc_now(),
        )
        session.add(row)

    row.run_id = run_id
    row.methodology = METHODOLOGY
    row.trend = view.technical.trend if view.technical else "unknown"
    row.rsi_state = view.technical.rsi_state if view.technical else "unknown"
    row.archetype = view.archetype.value
    row.open_events = len(view.events)
    row.summary = (
        f"close {view.technical.close if view.technical else '—'}; "
        f"{row.trend}; {row.rsi_state}"
    )
    session.flush()
    return row


def previous_snapshot(
    session: Session, symbol: str, before: date
) -> IntelligenceSnapshotRow | None:
    """The most recent snapshot strictly before a date.

    Strictly before, so today's snapshot is never compared with itself —
    which would report that nothing ever changes.
    """
    return session.scalar(
        select(IntelligenceSnapshotRow)
        .where(
            IntelligenceSnapshotRow.symbol == symbol.upper(),
            IntelligenceSnapshotRow.as_of < before,
        )
        .order_by(IntelligenceSnapshotRow.as_of.desc())
        .limit(1)
    )


# -- the engines --------------------------------------------------------


def brain_view(
    session: Session,
    settings: Settings,
    symbol: str,
    portfolio_id: str | None = None,
    since: date | None = None,
) -> BrainView | None:
    """What changed and why, for one security."""
    view = security.security_view(session, settings, symbol, portfolio_id=portfolio_id)
    if view is None:
        return None

    normalized = view.instrument.symbol
    cache = market_data.build_price_cache(settings)
    bars = cache.bars(normalized)
    window_start = since or (view.as_of - timedelta(days=30))

    prior = previous_snapshot(session, normalized, view.as_of)
    prior_close = cache.close_on(normalized, window_start)

    dna = (
        position_dna(session, portfolio_id, normalized)
        if portfolio_id
        else PositionDna(symbol=normalized)
    )

    recent_titles = [event.key_title for event in view.events]
    warnings = thesis_warnings(dna, recent_titles) if dna.invalidation else []

    events_since = [
        event for event in view.events if event.last_seen.date() >= window_start
    ]
    critical = 0
    if portfolio_id:
        critical = len(
            session.scalars(
                select(AlertRow).where(
                    AlertRow.portfolio_id == portfolio_id,
                    AlertRow.severity == Severity.CRITICAL.value,
                    AlertRow.symbols.like(f"%{normalized}%"),
                )
            ).all()
        )

    changes = what_changed(
        symbol=normalized,
        since=window_start,
        until=view.as_of,
        price_before=prior_close,
        price_after=view.technical.close if view.technical else None,
        trend_before=prior.trend if prior else None,
        trend_after=view.technical.trend if view.technical else None,
        new_events=len(events_since),
        critical_events=critical,
        thesis_flags=warnings,
    )

    move = None
    if len(bars) >= 2 and view.technical:
        today, yesterday = bars[-1], bars[-2]
        if yesterday.close:
            change = (today.close - yesterday.close) / yesterday.close
            benchmark_bars = cache.bars(settings.portfolio.default_benchmark_symbol)
            market = None
            if len(benchmark_bars) >= 2 and benchmark_bars[-2].close:
                market = (
                    benchmark_bars[-1].close - benchmark_bars[-2].close
                ) / benchmark_bars[-2].close
            move = explain_move(
                symbol=normalized,
                on=today.on,
                move=change,
                market_move=market,
                events=[
                    (event.key_title, _severity_of(event), event.best_source_class
                     == "regulatory_filing")
                    for event in events_since[:5]
                ],
                relative_volume=view.technical.relative_volume,
            )

    return BrainView(
        symbol=normalized, changes=changes, move=move, dna=dna, warnings=warnings
    )


def _severity_of(event: NewsEventRow) -> Severity:
    """Map a stored event to a severity for move attribution.

    Uses the number of financial dimensions it touched, which is the only
    severity-like signal the event row carries — the alert's own severity
    is portfolio-specific and this is not.
    """
    dimensions = len([part for part in event.dimensions.split(",") if part])
    if dimensions >= 3:
        return Severity.CRITICAL
    if dimensions >= 2:
        return Severity.IMPORTANT
    return Severity.WATCH


def refresh_snapshots(
    session: Session, settings: Settings, symbols: Sequence[str], run_id: str = ""
) -> dict[str, Any]:
    """Take a snapshot of every monitored security.

    Runs after the data stages so the snapshot reflects today's prices and
    filings rather than yesterday's.
    """
    taken = 0
    for symbol in symbols:
        if take_snapshot(session, settings, symbol, run_id=run_id) is not None:
            taken += 1
    return {"items": taken, "detail": f"{taken} snapshot(s)"}


def portfolio_changes(
    session: Session, settings: Settings, portfolio_id: str, symbols: Sequence[str]
) -> list[ChangeSet]:
    """What changed across a whole portfolio, quiet symbols omitted.

    Omitting the quiet ones is the point: a list that includes every
    holding every day is a list nobody reads, and the whole purpose of the
    engine is to be short.
    """
    results: list[ChangeSet] = []
    for symbol in symbols:
        view = brain_view(session, settings, symbol, portfolio_id=portfolio_id)
        if view is not None and not view.changes.is_quiet:
            results.append(view.changes)
    return results


def decimal_or_none(value: Any) -> Decimal | None:
    return value if isinstance(value, Decimal) else None
