"""The position intelligence page.

One security, from every angle the platform has, answering: what matters
about this right now?
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.services import portfolio as portfolio_service
from akmarkets.services import security as security_service
from akmarkets.web.identity import resolve_user

router = APIRouter(tags=["securities"])


@router.get("/securities/{symbol}", response_class=HTMLResponse)
def detail(request: Request, symbol: str, portfolio: str = "") -> Any:
    settings = cast(Settings, request.app.state.settings)
    database = cast(Database, request.app.state.database)
    templates = cast(Jinja2Templates, request.app.state.templates)

    with database.write() as session:
        identity = resolve_user(request, session, settings)
        if portfolio:
            # A symbol is public information, but a *position* in it is not.
            # The portfolio context is only honoured for someone entitled to
            # it; otherwise the page renders without holdings.
            try:
                portfolio_service.require_access(session, portfolio, identity.user.user_id)
            except (portfolio_service.NotFound, portfolio_service.AccessDenied) as exc:
                raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

        view = security_service.security_view(
            session, settings, symbol, portfolio_id=portfolio or None
        )
        if view is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"{symbol} is not an instrument here"
            )
        portfolios = portfolio_service.list_portfolios(session, identity.user.user_id)

    return templates.TemplateResponse(
        request,
        "security.html",
        {
            "title": view.instrument.symbol,
            "view": view,
            "portfolio_id": portfolio,
            "portfolios": portfolios,
            "identity": identity,
        },
    )
