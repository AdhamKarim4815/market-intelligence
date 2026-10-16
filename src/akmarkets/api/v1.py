"""The `/api/v1` JSON surface. A contract, not a convenience.

A future iOS build may sit on a phone for months. Once a field ships here it
cannot be removed or repurposed within v1 — additive changes only, and a
breaking change means `/api/v2` alongside this one, not a rewrite of it.

Four rules the shape follows, all of them for that client's benefit:

**Every response carries freshness.** `as_of` and `stale` are on the
envelope, not buried, so a client that has been offline knows whether what
it is showing is worth showing before the user reads it.

**Unknown is `null` and says why.** A missing portfolio total is `null` with
`incomplete: true` and the symbols responsible — never `0`, which a client
would happily render as a real number.

**Ids are stable and opaque.** They appear in deep links, so they are the
same after a restore from backup and mean nothing to look at.

**Lists are paginated from the first release.** Adding pagination later is a
breaking change, and doing it now costs one field.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from akmarkets import __version__
from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.domain.sessions import last_completed_session
from akmarkets.services import market_data, news, performance, risk
from akmarkets.services import portfolio as portfolio_service
from akmarkets.services import security as security_service
from akmarkets.services.identity import PerimeterMissing, resolve

router = APIRouter(prefix="/api/v1", tags=["api"])

API_VERSION = "1.0"

# A client older than this should be told to update rather than shown
# something it will misread. Bumped only on a genuine incompatibility.
MINIMUM_CLIENT = "1.0"


def _envelope(payload: dict[str, Any], as_of: date | None = None) -> dict[str, Any]:
    """Wrap a response with the freshness a client needs to judge it.

    `stale` is computed here rather than left to the client, because "is
    this current" depends on the market calendar and a phone should not need
    to know about market holidays to answer it.
    """
    settled = last_completed_session()
    return {
        "api_version": API_VERSION,
        "minimum_client": MINIMUM_CLIENT,
        "server_version": __version__,
        "as_of": as_of.isoformat() if as_of else None,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "stale": bool(as_of and as_of < settled),
        "data": payload,
    }


def _decimal(value: Decimal | None) -> str | None:
    """Decimals as strings, never as JSON numbers.

    A JSON number is a float in every client that parses it, and the whole
    stack keeps money exact precisely so it does not become one at the last
    step. A string round-trips into a client's own decimal type.
    """
    return None if value is None else str(value)


def _context(request: Request) -> tuple[Settings, Database]:
    return (
        cast(Settings, request.app.state.settings),
        cast(Database, request.app.state.database),
    )


def _identity(request: Request, session: Any, settings: Settings) -> Any:
    """Resolve the caller, translating a perimeter failure into a 403."""
    try:
        return resolve(session, request.headers, settings.env)
    except PerimeterMissing as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


def _guard(session: Any, portfolio_id: str, user_id: str) -> None:
    try:
        portfolio_service.require_access(session, portfolio_id, user_id)
    except portfolio_service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such portfolio") from exc
    except portfolio_service.AccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


@router.get("/portfolios")
def list_portfolios(request: Request) -> JSONResponse:
    settings, database = _context(request)
    with database.write() as session:
        identity = _identity(request, session, settings)
        folios = portfolio_service.list_portfolios(session, identity.user.user_id)
        payload = [
            {
                "id": folio.portfolio_id,
                "name": folio.name,
                "currency": folio.base_currency,
                "cost_basis_method": folio.cost_basis_method.value,
                "benchmark": folio.benchmark_symbol,
            }
            for folio in folios
        ]
    return JSONResponse(_envelope({"portfolios": payload}))


@router.get("/portfolios/{portfolio_id}")
def portfolio_detail(request: Request, portfolio_id: str) -> JSONResponse:
    settings, database = _context(request)
    with database.write() as session:
        identity = _identity(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id)

        folio = portfolio_service.get_portfolio(session, portfolio_id)
        cache = market_data.build_price_cache(settings)
        symbols = performance.portfolio_symbols(session, portfolio_id)
        prices, price_dates = market_data.closing_prices(cache, symbols)
        instruments = portfolio_service.list_instruments(
            session,
            [
                t.instrument_id
                for t in portfolio_service.list_transactions(session, portfolio_id)
                if t.instrument_id
            ],
        )
        by_instrument = {
            instrument_id: prices[instrument.symbol]
            for instrument_id, instrument in instruments.items()
            if instrument.symbol in prices
        }
        state = portfolio_service.portfolio_state(
            session, portfolio_id, prices=by_instrument
        )
        unpriced = [
            instruments[instrument_id].symbol
            for instrument_id in state.unpriced_instrument_ids
            if instrument_id in instruments
        ]
        payload = {
            "id": folio.portfolio_id,
            "name": folio.name,
            "currency": folio.base_currency,
            # Null, never zero. A client renders zero as a real number and
            # the user believes it.
            "total_value": _decimal(state.total_value),
            "cash": _decimal(state.cash_total),
            "cost_basis": _decimal(state.total_cost_basis),
            "unrealized_gain": _decimal(state.unrealized_gain),
            "realized_gain": _decimal(state.realized_gain),
            "incomplete": bool(unpriced),
            "unpriced_symbols": sorted(unpriced),
            "positions": [
                {
                    "instrument_id": position.instrument_id,
                    "symbol": instruments[position.instrument_id].symbol
                    if position.instrument_id in instruments
                    else None,
                    "quantity": _decimal(position.quantity),
                    "average_cost": _decimal(position.average_cost),
                    "last_price": _decimal(position.last_price),
                    "market_value": _decimal(position.market_value),
                    "unrealized_gain": _decimal(position.unrealized_gain),
                }
                for position in state.open_positions
            ],
        }
    # The valuation date, not the ledger's. `state.as_of` falls back to the
    # last *transaction* when no explicit date is given, so a portfolio last
    # traded in March would report March and be marked stale while being
    # valued at today's close — the opposite of what the field is for.
    valued_on = max(price_dates.values(), default=state.as_of)
    return JSONResponse(_envelope(payload, as_of=valued_on))


@router.get("/portfolios/{portfolio_id}/performance")
def portfolio_performance(
    request: Request, portfolio_id: str, period: str = "1m"
) -> JSONResponse:
    settings, database = _context(request)
    with database.write() as session:
        identity = _identity(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id)
        cache = market_data.build_price_cache(settings)
        report = performance.performance(session, cache, portfolio_id, period=period)
        payload = {
            "period": report.period,
            "start": report.start.isoformat(),
            "end": report.end.isoformat(),
            "time_weighted_return": _decimal(
                report.summary.time_weighted if report.summary else None
            ),
            "benchmark_symbol": report.benchmark.symbol,
            "benchmark_return": _decimal(report.benchmark.benchmark),
            "relative_return": _decimal(report.benchmark.relative),
            "incomplete": not report.is_complete,
            "unpriced_symbols": report.unpriced,
            "contributions": [
                {
                    "instrument_id": item.instrument_id,
                    "profit": _decimal(item.profit),
                    "weight": _decimal(item.weight),
                }
                for item in report.contributions
            ],
            "series": [
                {"on": point.on.isoformat(), "value": _decimal(point.value)}
                for point in report.series
            ],
        }
    return JSONResponse(_envelope(payload, as_of=report.end))


@router.get("/portfolios/{portfolio_id}/alerts")
def alerts(
    request: Request,
    portfolio_id: str,
    unread_only: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """The intelligence inbox.

    Paginated from the first release: adding pagination later would be a
    breaking change, and doing it now costs one field.
    """
    settings, database = _context(request)
    with database.write() as session:
        identity = _identity(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id)
        rows = news.inbox(
            session, portfolio_id, include_read=not unread_only, limit=limit + offset
        )
        page = rows[offset : offset + limit]
        payload = {
            "total_returned": len(page),
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > offset + limit,
            "unread_counts": news.unread_counts(session, portfolio_id),
            "alerts": [
                {
                    "id": alert.alert_id,
                    "severity": alert.severity,
                    "title": alert.title,
                    "body": alert.body,
                    "symbols": [s for s in alert.symbols.split(",") if s],
                    "score": alert.score,
                    "explanation": alert.explanation,
                    "created_at": alert.created_at.isoformat(),
                    "read": alert.read_at is not None,
                    # A stable path the client can open directly.
                    "deep_link": f"/portfolios/{portfolio_id}/inbox#{alert.alert_id}",
                }
                for alert in page
            ],
        }
    return JSONResponse(_envelope(payload))


@router.get("/securities/{symbol}")
def security(request: Request, symbol: str, portfolio: str = "") -> JSONResponse:
    settings, database = _context(request)
    with database.write() as session:
        identity = _identity(request, session, settings)
        if portfolio:
            _guard(session, portfolio, identity.user.user_id)
        view = security_service.security_view(
            session, settings, symbol, portfolio_id=portfolio or None
        )
        if view is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no instrument {symbol}")

        technical = view.technical
        payload = {
            "symbol": view.instrument.symbol,
            "name": view.instrument.name,
            "sector": view.instrument.sector,
            "bars_available": view.bars,
            "technical": None
            if technical is None
            else {
                "close": _decimal(technical.close),
                "trend": technical.trend,
                "rsi14": _decimal(technical.rsi14),
                "rsi_state": technical.rsi_state,
                "atr14": _decimal(technical.atr14),
                "relative_volume": _decimal(technical.relative_volume),
                "ma50": _decimal(technical.ma50),
                "ma200": _decimal(technical.ma200),
            },
            "valuation": None
            if view.valuation is None
            else {
                "archetype": view.valuation.archetype.value,
                "pe": _decimal(view.valuation.pe),
                "ps": _decimal(view.valuation.ps),
                "pb": _decimal(view.valuation.pb),
                "fcf_yield": _decimal(view.valuation.fcf_yield),
                # Both lists shipped: one is a statement about the company,
                # the other a gap in our data, and a client that conflates
                # them tells its user nothing.
                "not_applicable": list(view.valuation.not_applicable),
                "unavailable": list(view.valuation.unavailable),
            },
            "events": [
                {
                    "id": event.event_id,
                    "title": event.key_title,
                    "confirmation": event.confirmation,
                    "dimensions": [d for d in event.dimensions.split(",") if d],
                    "last_seen": event.last_seen.isoformat(),
                }
                for event in view.events
            ],
        }
    return JSONResponse(_envelope(payload, as_of=view.as_of))


@router.get("/portfolios/{portfolio_id}/risk")
def portfolio_risk(request: Request, portfolio_id: str) -> JSONResponse:
    settings, database = _context(request)
    with database.write() as session:
        identity = _identity(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id)
        report = risk.risk_report(session, settings, portfolio_id)
        payload = {
            "observations": report.observations,
            "portfolio_beta": _decimal(report.portfolio_beta),
            "daily_volatility": _decimal(report.volatility),
            "sharpe": _decimal(report.sharpe),
            "sortino": _decimal(report.sortino),
            "effective_positions": _decimal(
                report.concentration.effective_positions if report.concentration else None
            ),
            # Named, so a client cannot present a partial figure as a whole
            # one.
            "insufficient_history": list(report.insufficient_history),
            "weights": {
                symbol: _decimal(weight) for symbol, weight in report.weights.items()
            },
            "sector_exposure": {
                sector: _decimal(weight)
                for sector, weight in report.sector_exposure.items()
            },
            "clusters": [
                {
                    "members": list(cluster.members),
                    "average_correlation": _decimal(cluster.average_correlation),
                    "combined_weight": _decimal(cluster.combined_weight),
                }
                for cluster in report.clusters
            ],
        }
    return JSONResponse(_envelope(payload, as_of=report.as_of))
