"""System health: database, schema version, provider status.

This grows into the full data-health surface. What matters from the first
commit is the principle it exists to serve: the system never lets stale or
absent data masquerade as current data. A component with no news is
`unknown`, not `healthy`.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from akmarkets import __version__
from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.db.migrations import schema_status
from akmarkets.services import market_data

router = APIRouter(tags=["health"])


def _collect(request: Request) -> dict[str, Any]:
    settings = cast(Settings, request.app.state.settings)
    database = cast(Database, request.app.state.database)

    components: list[dict[str, Any]] = []

    try:
        info = database.check_connection()
        components.append(
            {
                "name": "database",
                "state": "healthy",
                "detail": f"{info['target']} as {info['user']}",
            }
        )
        reachable = True
    except Exception as exc:
        components.append(
            {"name": "database", "state": "failed", "detail": f"{type(exc).__name__}: {exc}"}
        )
        reachable = False

    if reachable:
        try:
            status = schema_status(database.engine, settings.project_root)
            components.append(
                {
                    "name": "schema",
                    "state": "healthy" if status.ok else "degraded",
                    "detail": status.describe(),
                }
            )
        except Exception as exc:
            components.append({"name": "schema", "state": "failed", "detail": str(exc)})
    else:
        components.append(
            {"name": "schema", "state": "unknown", "detail": "database unreachable"}
        )

    if reachable:
        # Provider state is read from what was recorded, with staleness
        # derived at read time -- a feed whose last attempt succeeded a week
        # ago is stale, not healthy, and only the reader knows what "a week
        # ago" means relative to the market calendar.
        try:
            with database.read() as session:
                for provider in market_data.provider_health(session):
                    detail = str(provider["message"]) or "—"
                    failures = provider["consecutive_failures"]
                    if failures:
                        detail = f"{failures} consecutive failures. {detail}"
                    components.append(
                        {
                            "name": f"{provider['category']}: {provider['provider']}",
                            "state": provider["state"],
                            "detail": detail,
                        }
                    )
                findings = market_data.open_findings(session, limit=50)
        except Exception as exc:
            components.append(
                {"name": "providers", "state": "unknown", "detail": str(exc)[:120]}
            )
            findings = []
    else:
        findings = []

    worst = "healthy"
    for component in components:
        state = component["state"]
        if state == "failed":
            worst = "failed"
            break
        if state in {"degraded", "unknown"} and worst == "healthy":
            worst = "degraded"

    return {
        "state": worst,
        "version": __version__,
        "env": settings.env,
        "components": components,
        "findings": [
            {
                "code": finding.code,
                "severity": finding.severity,
                "subject": finding.subject_id,
                "message": finding.message,
                "detected_at": finding.detected_at,
            }
            for finding in findings
        ],
    }


@router.get("/api/health")
def health(request: Request) -> JSONResponse:
    """Machine-readable health. Non-200 when something is actually wrong.

    A monitor that only ever gets 200 is not a monitor, so the status code
    carries the verdict rather than burying it in the body.
    """
    payload = _collect(request)
    code = 200 if payload["state"] == "healthy" else 503
    # `jsonable_encoder`, not the raw payload: the collected rows carry
    # real `datetime` objects (`last_success_at`, `detected_at`) and
    # `JSONResponse` uses plain `json.dumps`, which raises on them. This
    # endpoint therefore returned 500 whenever a provider had ever
    # reported — that is, on every machine with data, and never on the
    # empty database the tests used. The monitor was broken in exactly the
    # case it exists for.
    return JSONResponse(jsonable_encoder(payload), status_code=code)


@router.get("/health", response_class=HTMLResponse)
def health_page(request: Request) -> Any:
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "health.html",
        {"title": "System health", "health": _collect(request)},
    )
