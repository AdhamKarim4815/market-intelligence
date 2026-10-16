"""Everything the platform knows about one security, in one place.

The position intelligence page answers a single question — *what matters
about this right now?* — and this service assembles the answer: price
history, technical state, filed fundamentals, valuation, news, and how much
of the portfolio it represents.

It calculates nothing. Each part comes from the pure module that owns it,
so the page and the CLI cannot disagree, and every figure remains testable
without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.db.base import utc_now
from akmarkets.db.models import FundamentalRow, InstrumentRow, NewsEventRow
from akmarkets.domain.fundamentals import (
    Archetype,
    Period,
    Valuation,
    growth,
    latest,
    normalise,
)
from akmarkets.domain.fundamentals import value as value_company
from akmarkets.domain.indicators import TechnicalState, technical_state
from akmarkets.domain.records import Instrument, Position
from akmarkets.providers.base import Bar, DataState
from akmarkets.providers.edgar import EdgarProvider, Fact
from akmarkets.services import market_data
from akmarkets.services import portfolio as portfolio_service

_FIELDS = (
    "revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
    "cash", "total_debt", "total_assets", "total_equity", "operating_cash_flow",
    "capex", "shares_diluted",
)


@dataclass(frozen=True)
class SecurityView:
    """One security, from every angle the platform has.

    Every section is optional. A security with three weeks of history has no
    200-day average and a foreign issuer has no EDGAR filings, and both are
    reported as absent rather than filled in.
    """

    instrument: Instrument
    as_of: date
    bars: int
    technical: TechnicalState | None
    periods: list[Period]
    valuation: Valuation | None
    revenue_growth: Decimal | None
    position: Position | None
    portfolio_weight: Decimal | None
    events: list[NewsEventRow]

    @property
    def has_fundamentals(self) -> bool:
        return bool(self.periods)

    @property
    def archetype(self) -> Archetype:
        return self.valuation.archetype if self.valuation else Archetype.UNKNOWN


def store_fundamentals(
    session: Session, instrument_id: str, facts: list[Fact], source: str = "sec-edgar"
) -> int:
    """Persist normalised periods, updating rather than duplicating.

    EDGAR rate-limits and a company-facts response is megabytes, so
    re-fetching to render a page would be both slow and rude.
    """
    periods = normalise(facts)
    now = utc_now()
    for period in periods:
        row = session.get(
            FundamentalRow, (instrument_id, period.period_end, period.is_annual)
        )
        if row is None:
            row = FundamentalRow(
                instrument_id=instrument_id,
                period_end=period.period_end,
                is_annual=period.is_annual,
            )
            session.add(row)
        row.fiscal_year = period.fiscal_year
        row.fiscal_period = period.fiscal_period
        row.source = source
        row.retrieved_at = now
        for field in _FIELDS:
            setattr(row, field, getattr(period, field))
    session.flush()
    return len(periods)


def load_fundamentals(session: Session, instrument_id: str) -> list[Period]:
    rows = session.scalars(
        select(FundamentalRow)
        .where(FundamentalRow.instrument_id == instrument_id)
        .order_by(FundamentalRow.period_end)
    ).all()
    return [
        Period(
            period_end=row.period_end,
            fiscal_year=row.fiscal_year,
            fiscal_period=row.fiscal_period,
            is_annual=row.is_annual,
            **{field: getattr(row, field) for field in _FIELDS},
        )
        for row in rows
    ]


def refresh_fundamentals(
    session: Session,
    settings: Settings,
    symbols: list[str],
    run_id: str = "",
    provider: Any = None,
) -> dict[str, Any]:
    """Fetch filed figures for symbols that have them.

    A symbol absent from EDGAR — a foreign issuer, most ETFs — is recorded
    as a finding once rather than retried every night, because it is a fact
    about the security and will not change.
    """
    edgar = provider or EdgarProvider(
        user_agent=settings.edgar_user_agent,
        requests_per_second=settings.fundamentals.edgar_requests_per_second,
        timeout=settings.fundamentals.request_timeout_seconds,
    )
    stored = 0
    missing: list[str] = []
    failed: list[str] = []

    for symbol in symbols:
        instrument = session.scalar(
            select(InstrumentRow).where(InstrumentRow.symbol == symbol.upper())
        )
        if instrument is None:
            continue

        result = edgar.fetch_facts(symbol)
        if result.value:
            stored += store_fundamentals(session, instrument.instrument_id, result.value)
            market_data.resolve_findings(session, "fundamentals_unavailable", symbol)
        elif result.state is DataState.UNKNOWN:
            missing.append(symbol)
            market_data.record_finding(
                session,
                code="fundamentals_unavailable",
                severity="info",
                subject_type="symbol",
                subject_id=symbol,
                message=result.message,
                run_id=run_id,
            )
        else:
            failed.append(symbol)

    market_data.record_provider_result(
        session,
        provider=edgar.name,
        category="fundamentals",
        state=DataState.DEGRADED if failed else DataState.HEALTHY,
        message=f"{len(failed)} failed, {len(missing)} not listed" if failed or missing else "",
    )
    return {
        "items": stored,
        "degraded": bool(failed),
        "detail": (
            f"{stored} period(s); {len(missing)} symbol(s) not in EDGAR"
            if missing
            else f"{stored} period(s)"
        ),
    }


def security_view(
    session: Session,
    settings: Settings,
    symbol: str,
    portfolio_id: str | None = None,
) -> SecurityView | None:
    """Assemble everything known about one security."""
    instrument_row = session.scalar(
        select(InstrumentRow).where(InstrumentRow.symbol == symbol.strip().upper())
    )
    if instrument_row is None:
        return None
    instrument = instrument_row.to_domain()

    cache = market_data.build_price_cache(settings)
    bars: list[Bar] = cache.bars(instrument.symbol)
    technical = technical_state(bars) if bars else None
    periods = load_fundamentals(session, instrument.instrument_id)

    price = technical.close if technical else None
    valuation = (
        value_company(periods, price=price, sector=instrument.sector) if periods else None
    )

    position: Position | None = None
    weight: Decimal | None = None
    if portfolio_id:
        prices = {instrument.instrument_id: price} if price is not None else {}
        state = portfolio_service.portfolio_state(session, portfolio_id, prices=prices)
        position = next(
            (
                held
                for held in state.positions
                if held.instrument_id == instrument.instrument_id
            ),
            None,
        )
        # Weight against the whole portfolio needs every holding priced, so
        # it is None when any of them is not — rather than a share of a
        # total that is quietly missing a position.
        total = state.positions_value
        if position is not None and position.market_value is not None and total:
            weight = position.market_value / total

    events = list(
        session.scalars(
            select(NewsEventRow)
            .where(NewsEventRow.symbols.like(f"%{instrument.symbol}%"))
            .order_by(NewsEventRow.last_seen.desc())
            .limit(10)
        ).all()
    )

    return SecurityView(
        instrument=instrument,
        as_of=technical.as_of if technical else date.today(),
        bars=len(bars),
        technical=technical,
        periods=periods,
        valuation=valuation,
        revenue_growth=growth(periods, "revenue") if periods else None,
        position=position,
        portfolio_weight=weight,
        events=events,
    )


def latest_period(session: Session, instrument_id: str) -> Period | None:
    return latest(load_fundamentals(session, instrument_id))
