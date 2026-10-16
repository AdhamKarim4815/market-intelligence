"""The intelligence inbox.

Alerts persist and have state — read, archived, muted — because an alert
that vanishes after being delivered cannot be returned to, and the thing a
person most often wants is the item they glanced at yesterday.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.services import news as news_service
from akmarkets.services import portfolio as portfolio_service
from akmarkets.web.identity import resolve_user

router = APIRouter(tags=["inbox"])


def _context(request: Request) -> tuple[Settings, Database, Jinja2Templates]:
    return (
        cast(Settings, request.app.state.settings),
        cast(Database, request.app.state.database),
        cast(Jinja2Templates, request.app.state.templates),
    )


def _guard(session: Any, portfolio_id: str, user_id: str, write: bool = False) -> None:
    try:
        portfolio_service.require_access(session, portfolio_id, user_id, write=write)
    except portfolio_service.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such portfolio") from exc
    except portfolio_service.AccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


@router.get("/portfolios/{portfolio_id}/inbox", response_class=HTMLResponse)
def inbox(
    request: Request,
    portfolio_id: str,
    show: str = "unread",
) -> Any:
    settings, database, templates = _context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id)
        folio = portfolio_service.get_portfolio(session, portfolio_id)

        alerts = news_service.inbox(
            session,
            portfolio_id,
            include_read=show in {"all", "read"},
            include_archived=show == "all",
        )
        counts = news_service.unread_counts(session, portfolio_id)
        watched = news_service.watchlist(session, portfolio_id)

    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "title": f"{folio.name} · Inbox",
            "portfolio": folio,
            "alerts": alerts,
            "counts": counts,
            "watchlist": watched,
            "show": show,
            "identity": identity,
        },
    )


@router.post("/portfolios/{portfolio_id}/inbox/{alert_id}/{action}")
def act(
    request: Request, portfolio_id: str, alert_id: str, action: str
) -> RedirectResponse:
    """Mark an alert read, archived or muted.

    Muting also marks it read: the user has explicitly dismissed it, and
    leaving it in an unread count they just cleared would teach them the
    button does not work.
    """
    if action not in {"read", "unread", "archive", "mute", "unmute"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown action {action!r}")

    settings, database, _ = _context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id, write=True)

        alert = news_service.set_alert_state(
            session,
            alert_id,
            read={"read": True, "unread": False}.get(action),
            archived=True if action == "archive" else None,
            muted={"mute": True, "unmute": False}.get(action),
        )
        if alert is None or alert.portfolio_id != portfolio_id:
            # Real id, wrong portfolio. Refusing on the mismatch stops a
            # guessed id acting across a boundary the URL implied was
            # checked.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such alert here")

    return RedirectResponse(
        f"/portfolios/{portfolio_id}/inbox", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/portfolios/{portfolio_id}/watchlist")
def add_watch(
    request: Request,
    portfolio_id: str,
    symbol: Annotated[str, Form()],
    tier: Annotated[str, Form()] = "C",
    note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if not symbol.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a watchlist entry needs a symbol")
    if tier not in {"A", "B", "C", "D"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown tier {tier!r}")

    settings, database, _ = _context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id, write=True)
        news_service.add_to_watchlist(session, portfolio_id, symbol, tier=tier, note=note)

    return RedirectResponse(
        f"/portfolios/{portfolio_id}/inbox", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/portfolios/{portfolio_id}/watchlist/{symbol}/remove")
def remove_watch(request: Request, portfolio_id: str, symbol: str) -> RedirectResponse:
    settings, database, _ = _context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        _guard(session, portfolio_id, identity.user.user_id, write=True)
        news_service.remove_from_watchlist(session, portfolio_id, symbol)
    return RedirectResponse(
        f"/portfolios/{portfolio_id}/inbox", status_code=status.HTTP_303_SEE_OTHER
    )
