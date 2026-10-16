"""What every route needs off the request, in one place.

This existed three times — twice copied and once inlined — before there was
a fourth caller. Route modules should differ in what they render, not in
how they reach the application state.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request
from fastapi.templating import Jinja2Templates

from akmarkets.config import Settings
from akmarkets.db.engine import Database


def context(request: Request) -> tuple[Settings, Database, Jinja2Templates]:
    return (
        cast(Settings, request.app.state.settings),
        cast(Database, request.app.state.database),
        cast(Jinja2Templates, request.app.state.templates),
    )
