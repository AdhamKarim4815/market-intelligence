"""Portfolio views and transaction entry.

Every route follows the same shape: resolve the user, check access, call one
service, render. No calculation happens here — a figure on a page and the
same figure from the CLI come from one function, so they cannot drift apart.

Writes are POST-then-redirect. A dashboard that re-submits a trade when
someone hits refresh is a portfolio that grows phantom positions.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, cast

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.domain.money import MoneyError, to_decimal
from akmarkets.domain.records import CostBasisMethod, TransactionType
from akmarkets.services import dashboard as dashboard_service
from akmarkets.services import market_data, performance
from akmarkets.services import portfolio as portfolio_service
from akmarkets.web.identity import resolve_user

router = APIRouter(tags=["portfolios"])


def _context(request: Request) -> tuple[Settings, Database, Jinja2Templates]:
    return (
        cast(Settings, request.app.state.settings),
        cast(Database, request.app.state.database),
        cast(Jinja2Templates, request.app.state.templates),
    )


def _guard(session: Any, portfolio_id: str, user_id: str, write: bool = False) -> None:
    """Translate a domain access error into the right HTTP status.

    404 for an unknown portfolio and 403 for one that exists but is not
    yours. Returning 404 for both would be tidier but tells a caller less
    than they already know: the id came from somewhere.
    """
    try:
        portfolio_service.require_access(session, portfolio_id, user_id, write=write)
    except portfolio_service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such portfolio") from exc
    except portfolio_service.AccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


@router.get("/portfolios", response_class=HTMLResponse)
def index(request: Request) -> Any:
    settings, database, templates = _context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        portfolios = portfolio_service.list_portfolios(session, identity.user.user_id)
        summaries = []
        cache = market_data.build_price_cache(settings)
        for folio in portfolios:
            symbols = performance.portfolio_symbols(session, folio.portfolio_id)
            prices, _ = market_data.closing_prices(cache, symbols)
            by_instrument = dashboard_service.prices_by_instrument(
                session, folio.portfolio_id, prices
            )
            state = portfolio_service.portfolio_state(
                session, folio.portfolio_id, prices=by_instrument
            )
            summaries.append({"portfolio": folio, "state": state})

    return templates.TemplateResponse(
        request,
        "portfolios.html",
        {"title": "Portfolios", "summaries": summaries, "identity": identity},
    )


@router.post("/portfolios")
def create(
    request: Request,
    name: Annotated[str, Form()],
    base_currency: Annotated[str, Form()] = "USD",
    cost_basis_method: Annotated[str, Form()] = "fifo",
    benchmark_symbol: Annotated[str, Form()] = "SPY",
) -> RedirectResponse:
    settings, database, _ = _context(request)
    if not name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a portfolio needs a name")
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        folio = portfolio_service.create_portfolio(
            session,
            owner_user_id=identity.user.user_id,
            name=name,
            base_currency=base_currency,
            cost_basis_method=CostBasisMethod(cost_basis_method),
            benchmark_symbol=benchmark_symbol,
        )
    return RedirectResponse(
        f"/portfolios/{folio.portfolio_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/portfolios/{portfolio_id}", response_class=HTMLResponse)
def detail(request: Request, portfolio_id: str, period: str = "1m") -> Any:
    settings, database, templates = _context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id)

        folio = portfolio_service.get_portfolio(session, portfolio_id)
        cache = market_data.build_price_cache(settings)
        symbols = performance.portfolio_symbols(session, portfolio_id)
        prices, _ = market_data.closing_prices(cache, symbols)
        by_instrument = dashboard_service.prices_by_instrument(session, portfolio_id, prices)

        state = portfolio_service.portfolio_state(
            session, portfolio_id, prices=by_instrument
        )
        report = performance.performance(session, cache, portfolio_id, period=period)
        transactions = portfolio_service.list_transactions(session, portfolio_id)
        instruments = portfolio_service.list_instruments(
            session, [t.instrument_id for t in transactions if t.instrument_id]
        )
        findings = portfolio_service.validate_portfolio(session, portfolio_id)

    return templates.TemplateResponse(
        request,
        "portfolio_detail.html",
        {
            "title": folio.name,
            "portfolio": folio,
            "state": state,
            "report": report,
            "instruments": instruments,
            "transactions": list(reversed(transactions))[:50],
            "findings": findings,
            "periods": list(performance.PERIODS),
            "period": period,
            "transaction_types": [kind.value for kind in TransactionType],
            "identity": identity,
        },
    )


@router.post("/portfolios/{portfolio_id}/transactions")
def add_transaction(
    request: Request,
    portfolio_id: str,
    transaction_type: Annotated[str, Form()],
    occurred_on: Annotated[str, Form()],
    symbol: Annotated[str, Form()] = "",
    quantity: Annotated[str, Form()] = "0",
    price: Annotated[str, Form()] = "0",
    fees: Annotated[str, Form()] = "0",
    amount: Annotated[str, Form()] = "0",
    split_ratio: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Record one entry. Validation lives in the domain record, not here.

    Every field arrives as a string from the form and is coerced by the same
    parser the CSV importer and the CLI use, so a bad value is rejected
    identically whichever door it came through.
    """
    settings, database, _ = _context(request)
    try:
        kind = TransactionType(transaction_type)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown transaction type {transaction_type!r}"
        ) from exc

    try:
        when = date.fromisoformat(occurred_on)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "date must be YYYY-MM-DD") from exc

    with database.write() as session:
        identity = resolve_user(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id, write=True)

        instrument_id = None
        if kind.requires_instrument:
            if not symbol.strip():
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"{kind.value} needs a symbol"
                )
            instrument = portfolio_service.get_or_create_instrument(session, symbol)
            instrument_id = instrument.instrument_id

        try:
            portfolio_service.record_transaction(
                session,
                portfolio_id=portfolio_id,
                transaction_type=kind,
                occurred_on=when,
                instrument_id=instrument_id,
                quantity=to_decimal(quantity or "0"),
                price=to_decimal(price or "0"),
                fees=to_decimal(fees or "0"),
                amount=to_decimal(amount or "0"),
                split_ratio=to_decimal(split_ratio) if split_ratio.strip() else None,
                note=note,
                source="web",
            )
        except (MoneyError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # See-other rather than a rendered page: a refresh must not re-post the
    # trade and grow a phantom position.
    return RedirectResponse(
        f"/portfolios/{portfolio_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/portfolios/{portfolio_id}/transactions/{transaction_id}/delete")
def delete_transaction(
    request: Request, portfolio_id: str, transaction_id: str
) -> RedirectResponse:
    settings, database, _ = _context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id, write=True)
        existing = portfolio_service.get_transaction(session, transaction_id)
        if existing.portfolio_id != portfolio_id:
            # The id is real but belongs to another portfolio. Refusing on
            # the mismatch stops a guessed id from deleting across a
            # boundary the URL implied was checked.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such transaction here")
        portfolio_service.delete_transaction(session, transaction_id)
    return RedirectResponse(
        f"/portfolios/{portfolio_id}", status_code=status.HTTP_303_SEE_OTHER
    )
