"""Rates and the earnings calendar.

Both were collected, stored and refreshed by the pipeline for a day before
anything could see them without a terminal. This page is the whole of that
gap.

**It prints figures and arithmetic differences, and interprets nothing.**
No inversion signal, no regime label, no composite score. A yield-curve
call is exactly the kind of number this project forbids: one that cannot be
justified from a documented methodology — and this project has already
shipped one score that failed its own outcome test. The 10y−2y spread is a
subtraction the reader can check; "the curve is signalling a slowdown" is
not.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from akmarkets.services import dashboard as dashboard_service
from akmarkets.services import macro as macro_service
from akmarkets.services import market_data
from akmarkets.web.identity import resolve_user
from akmarkets.web.routes._common import context

router = APIRouter(tags=["markets"])

# The two spreads worth printing, as differences between stored tenors.
SPREADS = (("BC_10YEAR", "BC_2YEAR"), ("BC_10YEAR", "BC_3MONTH"))


@router.get("/markets", response_class=HTMLResponse)
def markets(request: Request, days: int = 30) -> Any:
    """The curve, the calendar, and the health of what feeds them."""
    settings, database, templates = context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        rates = macro_service.latest_rates(session)
        universe = set(
            dashboard_service.universe_of(
                session,
                [
                    folio.portfolio_id
                    for folio in _portfolios(session, identity.user.user_id)
                ],
            )
        )
        earnings = macro_service.upcoming_earnings(session, within_days=days)
        health = [
            row
            for row in market_data.provider_health(session)
            if row.get("category") in {"macro", "catalysts"}
        ]

    by_id = {snapshot.series_id: snapshot for snapshot in rates}
    spreads = [
        (long.name, short.name, long.value - short.value)
        for long_id, short_id in SPREADS
        if (long := by_id.get(long_id)) and (short := by_id.get(short_id))
    ]

    return templates.TemplateResponse(
        request,
        "markets.html",
        {
            "title": "Markets",
            "schema_error": request.app.state.schema_error,
            "rates": rates,
            "spreads": spreads,
            "earnings": earnings,
            "universe": universe,
            "health": health,
            "days": days,
        },
    )


def _portfolios(session: Any, user_id: str) -> list[Any]:
    from akmarkets.services import portfolio as portfolio_service

    return portfolio_service.list_portfolios(session, user_id)


def _decimal(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) else None
