"""Job history — did last night's refresh actually run?

Unanswerable from a browser until now: the status bar shows what is running
*this second* from an in-memory log, which tells you nothing about the
overnight job that failed while nobody was watching.

The page leads with the last *success* per job rather than the last run,
because those are different facts and only one of them is reassuring.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from akmarkets.services import ask as ask_service
from akmarkets.services import jobs as jobs_service
from akmarkets.web.identity import resolve_user
from akmarkets.web.routes._common import context

router = APIRouter(tags=["jobs"])


@router.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, limit: int = 50) -> Any:
    settings, database, templates = context(request)
    with database.write() as session:
        resolve_user(request, session, settings)
        history = jobs_service.run_history(session, limit=limit)
        spent = ask_service.spent_today(session)
        by_feature = ask_service.cost_by_feature(session, days=30)

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "title": "Jobs",
            "schema_error": request.app.state.schema_error,
            "history": history,
            "spent_today": spent,
            "ceiling": settings.ai.daily_cost_ceiling_usd,
            "by_feature": by_feature,
        },
    )
