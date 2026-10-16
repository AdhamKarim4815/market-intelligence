"""Declarative base and the timestamp conventions every table follows.

Every table in this application is prefixed `mi_`. When the database is
shared with other applications (a single hosted Postgres project often is),
the runtime role for this application is granted access to the `mi_`
namespace and nothing else. The prefix is therefore a security boundary, not
a naming preference.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase

TABLE_PREFIX = "mi_"


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    """Every timestamp stored by this application is UTC.

    Presentation converts to the user's zone, and market events are rendered
    in the exchange's. Storage never is.
    """
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a timestamp read back from storage.

    SQLite has no native timestamp type and hands back naive datetimes even
    from a `DateTime(timezone=True)` column. Domain records reject naive
    timestamps on purpose -- an ambiguous timestamp in a system that reasons
    about market sessions is a bug waiting to happen -- so normalising once
    here beats normalising at every call site.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def new_id() -> str:
    """A stable, client-generatable identifier.

    UUIDv4 as text rather than a database sequence, because these ids appear
    in deep links and in a future iOS client's local cache. An id must mean
    the same thing after a restore from backup, and must be creatable by a
    client that has not yet reached the server.
    """
    return str(uuid.uuid4())
