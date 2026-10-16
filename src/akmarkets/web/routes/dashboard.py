"""The overview page.

Lived inline in `create_app` while it was a placeholder. It is a real page
now, and `app.py`'s own docstring says routes belong in routers.

Two behaviours here are deliberate and both are about failing in the right
direction.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from akmarkets import __version__
from akmarkets.services import dashboard as dashboard_service
from akmarkets.services import performance
from akmarkets.services import portfolio as portfolio_service
from akmarkets.web.identity import resolve_user
from akmarkets.web.routes._common import context

router = APIRouter(tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, period: str = "1m") -> Any:
    """What the portfolio is worth, what needs attention, and what broke.

    **A schema problem short-circuits the assembly.** Without that, an
    unmigrated database turns this page into a 500 — and the one surface
    that could explain the outage becomes its first casualty. The banner is
    the answer, not a stack trace.

    **A 403 is re-raised, never rendered.** `resolve_user` refuses a
    request with no Access header when `env == "production"`. Catching that
    into a friendly banner would serve the portfolio to anything that
    reached the origin directly, which is the exact failure
    `services.identity` exists to prevent.
    """
    settings, database, templates = context(request)
    schema_error = request.app.state.schema_error
    view = None
    identity = None

    if schema_error is None:
        try:
            # write(), not read(): resolving an identity creates the user
            # row on first sight, as every other identified route does.
            with database.write() as session:
                identity = resolve_user(request, session, settings)
                view = dashboard_service.overview(
                    session, settings, identity.user.user_id, period=period
                )
        except HTTPException:
            raise
        except Exception as exc:
            # Anything else becomes the banner rather than a 500, for the
            # same reason as the schema check above.
            schema_error = f"overview unavailable: {type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "title": "Overview",
            "schema_error": schema_error,
            "version": __version__,
            "overview": view,
            "identity": identity,
            "period": period,
            "periods": list(performance.PERIODS),
        },
    )


@router.get("/inbox")
def inbox_redirect(request: Request) -> RedirectResponse:
    """Resolve the portfolio-scoped inbox from a static nav link.

    Inbox and risk both need a portfolio id, which is why they were
    unreachable from the global nav and therefore, in practice, unreachable.
    A redirect keeps the id out of the nav without hiding the pages.
    """
    return _resolve(request, "inbox")


@router.get("/risk")
def risk_redirect(request: Request) -> RedirectResponse:
    return _resolve(request, "risk")


def _resolve(request: Request, page: str) -> RedirectResponse:
    settings, database, _ = context(request)
    with database.write() as session:
        identity = resolve_user(request, session, settings)
        folios = portfolio_service.list_portfolios(session, identity.user.user_id)
    # With exactly one portfolio the destination is unambiguous. With none
    # or several it is a choice, and guessing would send the reader to a
    # page about a portfolio they did not ask for.
    if len(folios) == 1:
        return RedirectResponse(
            f"/portfolios/{folios[0].portfolio_id}/{page}", status_code=303
        )
    return RedirectResponse("/portfolios", status_code=303)
