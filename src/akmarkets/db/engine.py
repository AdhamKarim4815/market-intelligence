"""Database connection: URL repair, backend-appropriate pooling, sessions.

The two backends are deliberately not equivalent in how they are reached.
Locally this is a SQLite file under `var/` and needs no credentials at all,
because a developer must be able to `uv sync` and run the project without
Supabase access. In production it is a hosted Postgres such as Supabase,
reached through the *session* pooler on port 5432.

That pooler choice is not arbitrary and is worth stating so it is not
"simplified" later: the direct host `db.<ref>.supabase.co` publishes only an
AAAA record, and GitHub Actions runners are IPv4-only, so a direct
connection fails DNS resolution before it ever reaches authentication. The
pooler publishes A records. The `aws-0`/`aws-1` prefix varies per project
even within one region.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

POSTGRES_PREFIXES = ("postgresql", "postgres:")


def is_postgres_url(url: str) -> bool:
    return url.strip().startswith(POSTGRES_PREFIXES)


def normalize_database_url(url: str) -> str:
    """Repair a Postgres URL whose password was pasted raw.

    Supabase hands out connection strings with a `[YOUR-PASSWORD]`
    placeholder, and real passwords routinely contain characters that are
    structural in a URL. `@` is the dangerous one: standard parsers split
    credentials from the host on the *first* `@`, so `...://user:pa@ss@host/db`
    is read as host `ss@host` and the connection dies with "failed to resolve
    host" -- naming a host nobody typed, which makes a quoting bug look like
    a DNS outage.

    Splitting on the *last* `@` recovers the intended boundary, since the
    host section can never contain one. The password is then percent-encoded,
    and surrounding whitespace and quotes are stripped, because a value
    copied into a CI secret box frequently carries a trailing newline.

    A password already containing `%` is left alone: it is assumed encoded,
    and re-encoding would corrupt it.
    """
    url = url.strip().strip("'\"").replace("\n", "").replace("\r", "")
    if not is_postgres_url(url):
        return url

    scheme, separator, rest = url.partition("://")
    if not separator:
        return url
    # Split on the last `@` *before* splitting off the path: a password may
    # contain `/` as legitimately as `@`, and taking the path first would cut
    # the credentials in half.
    credentials, at_sign, host_and_path = rest.rpartition("@")
    if not at_sign:
        return url
    host, slash, tail = host_and_path.partition("/")

    user, colon, password = credentials.partition(":")
    if colon and password and "%" not in password:
        password = quote(password, safe="")
    credentials = f"{user}:{password}" if colon else user
    return f"{scheme}://{credentials}@{host}{slash}{tail}"


def describe_database_url(url: str) -> str:
    """A connection target safe to print: never the password.

    Failures here are almost always the host, port or user, and a log line
    showing them turns a mystifying error into an obvious one.
    """
    url = normalize_database_url(url)
    if not is_postgres_url(url):
        return url
    _, _, rest = url.partition("://")
    credentials, at_sign, host_and_path = rest.rpartition("@")
    host, _, tail = host_and_path.partition("/")
    user = credentials.partition(":")[0] if at_sign else ""
    database = tail.partition("?")[0] or "(default)"
    return f"host={host} user={user or '(none)'} database={database}"


def engine_options(database_url: str) -> dict[str, Any]:
    """Connection settings appropriate to the backend.

    Local SQLite needs nothing. Supabase sits behind a connection pooler
    that drops idle connections, so connections are pre-pinged and recycled
    well inside that window; a scheduled ingest that runs for minutes would
    otherwise fail on a stale socket. The pooler also cannot hold
    server-side prepared statements, so those are disabled.

    The pool is deliberately small. A hosted database instance may be shared
    with other applications and allows a limited number of connections in
    total. Concurrency in this application is in HTTP fetching, not SQL, so a
    larger pool would reserve capacity from its neighbours for nothing.
    """
    if not is_postgres_url(database_url):
        return {}
    return {
        "pool_pre_ping": True,
        "pool_recycle": 240,
        "pool_size": 3,
        "max_overflow": 2,
        "connect_args": {"prepare_threshold": None},
    }


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """WAL so a background job and a web request can read while one writes.

    `foreign_keys=ON` is not the SQLite default and has to be set per
    connection. Without it the transaction/portfolio relationships are
    decorative, and the local backend would accept rows Postgres rejects --
    the exact class of bug that only shows up in production.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    """Owns the engine and hands out sessions. Creates no schema, ever.

    Schema changes arrive only through the migration files in `migrations/`,
    applied by an owner credential. This class deliberately has no
    `create_all`: the runtime role is not permitted to alter structure, and
    an application that quietly repairs its own schema on startup is how a
    production table gets dropped by a deploy.
    """

    def __init__(self, database_url: str) -> None:
        database_url = normalize_database_url(database_url)
        self.database_url = database_url
        if database_url.startswith("sqlite:///"):
            path = Path(database_url.removeprefix("sqlite:///"))
            path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            database_url, future=True, **engine_options(database_url)
        )
        if database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", _configure_sqlite)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @property
    def is_postgres(self) -> bool:
        return is_postgres_url(self.database_url)

    @contextmanager
    def read(self) -> Iterator[Session]:
        with self.sessions() as session:
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        with self.sessions.begin() as session:
            yield session

    def check_connection(self) -> dict[str, str]:
        """Prove the database is reachable and say who we connected as.

        Run first in CI, before anything that could be blamed on the
        application. Returns identity, never the password.
        """
        with self.engine.connect() as connection:
            if self.is_postgres:
                version = connection.execute(text("select version()")).scalar_one()
                user = connection.execute(text("select current_user")).scalar_one()
                database = connection.execute(text("select current_database()")).scalar_one()
            else:
                version = connection.execute(text("select sqlite_version()")).scalar_one()
                version = f"SQLite {version}"
                user, database = "(sqlite)", self.database_url
        return {
            "target": describe_database_url(self.database_url),
            "version": str(version),
            "user": str(user),
            "database": str(database),
        }

    def dispose(self) -> None:
        self.engine.dispose()
