"""The portfolio risk page."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.services import portfolio as portfolio_service
from akmarkets.services import risk as risk_service
from akmarkets.web.identity import resolve_user

router = APIRouter(tags=["risk"])


@router.get("/portfolios/{portfolio_id}/risk", response_class=HTMLResponse)
def risk_page(request: Request, portfolio_id: str) -> Any:
    settings = cast(Settings, request.app.state.settings)
    database = cast(Database, request.app.state.database)
    templates = cast(Jinja2Templates, request.app.state.templates)

    with database.write() as session:
        identity = resolve_user(request, session, settings)
        try:
            portfolio_service.require_access(session, portfolio_id, identity.user.user_id)
        except portfolio_service.NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such portfolio") from exc
        except portfolio_service.AccessDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

        folio = portfolio_service.get_portfolio(session, portfolio_id)
        report = risk_service.risk_report(session, settings, portfolio_id)

    return templates.TemplateResponse(
        request,
        "risk.html",
        {
            "title": f"{folio.name} · Risk",
            "portfolio": folio,
            "report": report,
            "identity": identity,
        },
    )
