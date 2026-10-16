"""FastAPI adapter over `akmarkets.services.identity`.

The logic lives in the service so the JSON API can use it too — the layering
test refuses an `api` module that imports from `web`, and it was right to.
This file exists only to pull headers off a request and translate a refusal
into an HTTP status.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.services.identity import (
    ACCESS_EMAIL_HEADER,
    ACCESS_JWT_HEADER,
    LOCAL_DEVELOPER_EMAIL,
    Identity,
    PerimeterMissing,
    resolve,
)

__all__ = [
    "ACCESS_EMAIL_HEADER",
    "ACCESS_JWT_HEADER",
    "LOCAL_DEVELOPER_EMAIL",
    "Identity",
    "resolve_user",
]


def resolve_user(request: Request, session: Session, settings: Settings) -> Identity:
    try:
        return resolve(session, request.headers, settings.env)
    except PerimeterMissing as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
