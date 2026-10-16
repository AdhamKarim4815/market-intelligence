"""FastAPI application factory.

Routes live in `akmarkets.web.routes.*` as `APIRouter`s rather than as
closures inside this factory. Putting every route inline in one
`create_app()` grows into a file nobody can review; this application has a
large surface, so the split happens at the start rather than after it hurts.

Nothing here calculates anything. A route reads request parameters, calls
one application service, and renders. That is what keeps a figure on a page
identical to the same figure from the CLI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from akmarkets import __version__
from akmarkets.activity import ACTIVITY
from akmarkets.api import v1 as api_v1
from akmarkets.config import Settings, get_settings
from akmarkets.db.engine import Database
from akmarkets.db.migrations import MigrationError, schema_status
from akmarkets.web import filters
from akmarkets.web.routes import activity as activity_routes
from akmarkets.web.routes import dashboard as dashboard_routes
from akmarkets.web.routes import health as health_routes
from akmarkets.web.routes import inbox as inbox_routes
from akmarkets.web.routes import jobs as jobs_routes
from akmarkets.web.routes import markets as markets_routes
from akmarkets.web.routes import portfolios as portfolio_routes
from akmarkets.web.routes import risk as risk_routes
from akmarkets.web.routes import securities as security_routes

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    filters.register(templates)
    return templates


def create_app(settings: Settings | None = None, verify_schema: bool = True) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved.database_url)
        application.state.settings = resolved
        application.state.database = database
        application.state.activity = ACTIVITY
        application.state.schema_error = None
        # Startup verifies the schema and refuses to treat a mismatch as
        # normal. It never migrates: the runtime role has no DDL privileges,
        # and an application that repairs its own schema is how a production
        # table gets dropped by a deploy.
        #
        # The error is held on state rather than raised. A process that dies
        # in lifespan takes the health page down with it, so the one surface
        # that could explain the outage is the first casualty of it -- which
        # is exactly backwards. Every failure mode here (unreachable
        # database, revoked credential, pending migration) ends up as a
        # banner on every page and a `failed` component on /health.
        if verify_schema:
            try:
                status = schema_status(database.engine, resolved.project_root)
                if not status.ok:
                    raise MigrationError(status.describe())
            except MigrationError as exc:
                application.state.schema_error = str(exc)
                ACTIVITY.emit("schema", "schema check failed", level="error", detail=str(exc))
            except Exception as exc:
                message = f"database unreachable: {type(exc).__name__}: {exc}"
                application.state.schema_error = message
                ACTIVITY.emit("schema", "schema check failed", level="error", detail=message)
        try:
            yield
        finally:
            database.dispose()

    application = FastAPI(
        title="Market Intelligence",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.templates = build_templates()
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    application.include_router(health_routes.router)
    application.include_router(activity_routes.router)
    application.include_router(portfolio_routes.router)
    application.include_router(inbox_routes.router)
    application.include_router(security_routes.router)
    application.include_router(dashboard_routes.router)
    application.include_router(markets_routes.router)
    application.include_router(jobs_routes.router)
    application.include_router(risk_routes.router)
    application.include_router(api_v1.router)

    return application
