"""Macro rates and the earnings calendar: refresh, store, read back.

Both feeds are keyless and official, so this service has no credential
handling and no quota to respect — see `providers/macro.py` for why FRED
and Finnhub were measured and rejected in favour of the primary sources.

The two halves are kept apart because they fail apart. A Treasury outage
must not stop the earnings calendar from refreshing, and neither should
stop a price refresh, which is why they are separate pipeline stages
rather than one "extras" stage.
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
from akmarkets.db.base import utc_now
from akmarkets.db.models import EarningsEventRow, MacroObservationRow
from akmarkets.providers.base import DataState
from akmarkets.providers.macro import (
    EarningsEvent,
    MacroSeries,
    NasdaqEarningsProvider,
    TreasuryProvider,
)
from akmarkets.services import market_data

# How far back a refresh keeps yield-curve history. Two years, because the
# question people actually ask is "where are rates versus a year ago", and
# answering it across a January boundary needs more than the current year.
YIELD_HISTORY_YEARS = 2

# How far ahead the earnings calendar is fetched. Fourteen days is one
# request per weekday and covers the horizon over which a report is worth
# knowing about in advance; beyond that the date is provisional anyway.
CATALYST_HORIZON_DAYS = 14

_TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class RateSnapshot:
    """One tenor's latest yield and how it has moved.

    `change_1m` and `change_1y` are `None` rather than zero when the stored
    history is too short to support them. "The 10-year is unchanged on the
    year" derived from three weeks of data is not a smaller version of the
    truth, it is a false statement.
    """

    series_id: str
    name: str
    observed_on: date
    value: Decimal
    unit: str
    change_1m: Decimal | None = None
    change_1y: Decimal | None = None


def _store_series(session: Session, series: Sequence[MacroSeries], source: str) -> int:
    """Upsert observations. Treasury revises, so an existing day is updated."""
    stored = 0
    for item in series:
        for observed_on, value in item.points:
            row = session.get(MacroObservationRow, (item.series_id, observed_on))
            if row is None:
                row = MacroObservationRow(
                    series_id=item.series_id, observed_on=observed_on
                )
                session.add(row)
            row.value = value
            row.unit = item.unit
            row.name = item.name
            row.source = source
            row.retrieved_at = utc_now()
            stored += 1
    return stored


def refresh_macro(
    session: Session,
    settings: Settings,
    run_id: str = "",
    provider: TreasuryProvider | None = None,
) -> dict[str, Any]:
    """Bring the yield curve up to date.

    Reports degraded rather than raising when the feed is partly available:
    a curve missing its 30-year point is still a usable curve, and treating
    it as a failure would mean storing nothing at all.
    """
    treasury = provider or TreasuryProvider()
    result = treasury.fetch_yield_curve(years=YIELD_HISTORY_YEARS)
    market_data.record_provider_result(
        session,
        provider=treasury.name,
        category="macro",
        state=result.state,
        message=result.message,
    )
    if result.state is DataState.FAILED or not result.value:
        return {
            "items": 0,
            "degraded": True,
            "detail": f"yield curve unavailable: {result.message[:80]}",
        }

    stored = _store_series(session, result.value, treasury.name)
    tenors = len(result.value)
    return {
        "items": stored,
        "degraded": result.state is DataState.DEGRADED,
        "detail": f"{tenors} tenor(s), {stored} observation(s)",
    }


def refresh_catalysts(
    session: Session,
    settings: Settings,
    symbols: Sequence[str],
    run_id: str = "",
    today: date | None = None,
    provider: NasdaqEarningsProvider | None = None,
) -> dict[str, Any]:
    """Store upcoming earnings dates for the symbols actually held.

    Scoped to the universe rather than fetching the whole calendar: the
    market reports thousands of times a quarter and none of it matters
    unless it is something you own or watch.
    """
    if not symbols:
        return {"items": 0, "detail": "no symbols"}

    nasdaq = provider or NasdaqEarningsProvider()
    start = today or date.today()
    # Weekdays only. Companies do not report at the weekend, and asking
    # costs a request that can only return nothing.
    horizon = [
        day
        for offset in range(CATALYST_HORIZON_DAYS)
        if (day := start + timedelta(days=offset)).weekday() < 5
    ]
    result = nasdaq.fetch_for_symbols(symbols, horizon)
    market_data.record_provider_result(
        session,
        provider=nasdaq.name,
        category="catalysts",
        state=result.state,
        message=result.message,
    )
    if result.state is DataState.FAILED:
        return {
            "items": 0,
            "degraded": True,
            "detail": f"calendar unavailable: {result.message[:80]}",
        }

    stored = _store_events(session, result.value or [], nasdaq.name)
    return {
        "items": stored,
        "degraded": result.state is DataState.DEGRADED,
        "detail": f"{stored} scheduled report(s) in the next {CATALYST_HORIZON_DAYS} days",
    }


def _store_events(session: Session, events: Sequence[EarningsEvent], source: str) -> int:
    """Upsert by (symbol, date). A revised estimate corrects the row."""
    for event in events:
        row = session.get(EarningsEventRow, (event.symbol, event.on))
        if row is None:
            row = EarningsEventRow(symbol=event.symbol, reports_on=event.on)
            session.add(row)
        row.company = event.company
        row.eps_forecast = event.eps_forecast
        row.estimate_count = event.estimate_count
        row.last_year_eps = event.last_year_eps
        row.time_of_day = event.time_of_day
        row.is_confirmed = event.is_confirmed
        row.source = source
        row.retrieved_at = utc_now()
    return len(events)


def latest_rates(session: Session, since: date | None = None) -> list[RateSnapshot]:
    """The current curve, with changes computed from stored history.

    `since` bounds the scan. Unbounded it reads every observation ever
    stored — 1,600 rows today and growing by four a session — which is fine
    from a CLI and wasteful on a page that renders on every visit.

    **Do not pass a window shorter than a year and a margin.** `_change`
    refuses to answer a 1-year question from 11 months of data, and it is
    right to; a tight window would not raise, it would quietly render every
    1-year column as an em dash, which reads as missing data rather than as
    a badly chosen query.
    """
    query = select(MacroObservationRow).order_by(
        MacroObservationRow.series_id, MacroObservationRow.observed_on
    )
    if since is not None:
        query = query.where(MacroObservationRow.observed_on >= since)
    rows = list(session.scalars(query).all())
    by_series: dict[str, list[MacroObservationRow]] = {}
    for row in rows:
        by_series.setdefault(row.series_id, []).append(row)

    snapshots: list[RateSnapshot] = []
    for series_id, points in by_series.items():
        latest = points[-1]
        snapshots.append(
            RateSnapshot(
                series_id=series_id,
                name=latest.name,
                observed_on=latest.observed_on,
                value=latest.value,
                unit=latest.unit,
                change_1m=_change(points, days=30),
                change_1y=_change(points, days=365),
            )
        )
    return sorted(snapshots, key=lambda s: s.series_id)


def _change(points: Sequence[MacroObservationRow], days: int) -> Decimal | None:
    """Change against the observation closest to `days` ago, or None.

    None when nothing old enough is stored, rather than falling back to the
    oldest available point — which would silently answer a one-year
    question with three weeks of data.
    """
    if len(points) < 2:
        return None
    latest = points[-1]
    target = latest.observed_on.toordinal() - days
    earlier = [p for p in points if p.observed_on.toordinal() <= target]
    if not earlier:
        return None
    return latest.value - earlier[-1].value


def upcoming_earnings(
    session: Session,
    symbols: Sequence[str] = (),
    within_days: int = CATALYST_HORIZON_DAYS,
    today: date | None = None,
) -> list[EarningsEventRow]:
    """Scheduled reports between today and the horizon, soonest first."""
    start = today or date.today()
    query = select(EarningsEventRow).where(
        EarningsEventRow.reports_on >= start,
        EarningsEventRow.reports_on <= start + timedelta(days=within_days),
    )
    if symbols:
        query = query.where(EarningsEventRow.symbol.in_([s.upper() for s in symbols]))
    return list(
        session.scalars(query.order_by(EarningsEventRow.reports_on, EarningsEventRow.symbol)).all()
    )


def trim(value: Decimal) -> Decimal:
    """Drop storage padding for display without going exponential.

    The column keeps eight decimal places, so a published yield of 4.65
    reads back as `4.65000000` and an unchanged rate as `0E-8`. Bare
    `normalize()` fixes both and introduces a worse bug — it turns 100 into
    `1E+2` — so the exponent is pulled back for whole numbers.

    Display only. Nothing rounds the stored value, because basis points are
    the unit these are argued in.
    """
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return normalized


def describe_rate(snapshot: RateSnapshot) -> str:
    """One line, with the move only where it can be supported.

    The unit is always printed. A rate and a change in a rate are both
    small numbers and mistaking one for the other is silent.
    """
    # Rates are quoted to two places and read in columns, so a yield of
    # 4.20 must not print as "4.2" next to one printing as "4.65". `trim`
    # alone is right for an estimate and wrong here.
    def rate(value: Decimal) -> Decimal:
        return trim(value).quantize(_TWO_PLACES) if snapshot.unit == "percent" else trim(value)

    parts = [f"{snapshot.name} {rate(snapshot.value)}%"]
    if snapshot.change_1m is not None:
        parts.append(f"1m {rate(snapshot.change_1m):+}")
    if snapshot.change_1y is not None:
        parts.append(f"1y {rate(snapshot.change_1y):+}")
    parts.append(f"as of {snapshot.observed_on}")
    return "  ".join(parts)


def describe_earnings(row: EarningsEventRow) -> str:
    """One line for a scheduled report.

    An unconfirmed session is stated as unconfirmed rather than rendered as
    a time, because a catalyst calendar that implies a known moment can be
    wrong by up to sixteen hours.
    """
    when = row.time_of_day if row.is_confirmed else "session unconfirmed"
    estimate = (
        f"est {trim(row.eps_forecast)}" if row.eps_forecast is not None else "no consensus"
    )
    return f"{row.reports_on}  {row.symbol:<6} {estimate:<16} ({when})"

