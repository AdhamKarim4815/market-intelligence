"""Connection URL handling, pooling, and the exact-decimal column types."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Column, MetaData, String, Table, insert, select

from akmarkets.db.engine import (
    Database,
    describe_database_url,
    engine_options,
    is_postgres_url,
    normalize_database_url,
)
from akmarkets.db.types import MoneyColumn, QuantityColumn

SUPABASE = (
    "postgresql+psycopg://postgres.exampleprojectref:{password}"
    "@pooler.example.com:5432/postgres"
)


def test_password_containing_an_at_sign_keeps_the_right_host() -> None:
    """Splitting on the first `@` names a host nobody typed.

    That failure surfaces as "could not translate host name", which sends
    you looking at DNS for an hour when the bug is quoting. Splitting on the
    last `@` is what makes it right.
    """
    url = normalize_database_url(SUPABASE.format(password="pa@ss"))
    assert "@pooler.example.com:5432/postgres" in url
    assert "pa%40ss" in url


def test_password_containing_a_slash_is_not_cut_by_the_path() -> None:
    url = normalize_database_url(SUPABASE.format(password="a/b"))
    assert url.endswith("/postgres")
    assert "a%2Fb" in url


def test_already_encoded_password_is_left_alone() -> None:
    """Re-encoding a `%` would corrupt a password that was already escaped."""
    url = normalize_database_url(SUPABASE.format(password="pa%40ss"))
    assert "pa%40ss" in url
    assert "pa%2540ss" not in url


def test_trailing_newline_from_a_ci_secret_box_is_stripped() -> None:
    url = normalize_database_url("  " + SUPABASE.format(password="x") + "\n")
    assert not url.endswith("\n")
    assert url.startswith("postgresql+psycopg://")


def test_sqlite_urls_pass_through_untouched() -> None:
    assert normalize_database_url("sqlite:///var/akmarkets.sqlite3") == (
        "sqlite:///var/akmarkets.sqlite3"
    )


def test_describe_never_reveals_the_password() -> None:
    described = describe_database_url(SUPABASE.format(password="hunter2"))
    assert "hunter2" not in described
    assert "host=pooler.example.com:5432" in described
    assert "user=postgres.exampleprojectref" in described


def test_postgres_gets_pooler_safe_options_and_sqlite_gets_none() -> None:
    """Supabase's pooler cannot hold server-side prepared statements."""
    options = engine_options(SUPABASE.format(password="x"))
    assert options["connect_args"] == {"prepare_threshold": None}
    assert options["pool_pre_ping"] is True
    assert options["pool_recycle"] == 240
    assert options["pool_size"] == 3
    assert engine_options("sqlite:///var/x.sqlite3") == {}


def test_sqlite_enforces_foreign_keys(database: Database) -> None:
    """Not the SQLite default. Without it the local backend accepts rows
    Postgres rejects, which is a bug that only appears in production."""
    with database.engine.connect() as connection:
        enabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    assert enabled == 1


def test_decimal_columns_round_trip_exactly(database: Database) -> None:
    """The reason these types exist: SQLAlchemy's Numeric on SQLite goes
    through a float, and a portfolio compared to a broker statement cannot
    afford that."""
    metadata = MetaData()
    table = Table(
        "probe",
        metadata,
        Column("id", String(8), primary_key=True),
        Column("amount", MoneyColumn()),
        Column("shares", QuantityColumn()),
    )
    metadata.create_all(database.engine)

    amount = Decimal("12345678.1234")
    shares = Decimal("0.12345678")
    with database.engine.begin() as connection:
        connection.execute(insert(table).values(id="a", amount=amount, shares=shares))

    with database.engine.connect() as connection:
        row = connection.execute(select(table.c.amount, table.c.shares)).one()

    assert row[0] == amount
    assert row[1] == shares
    assert isinstance(row[0], Decimal)


def test_decimal_columns_accept_none(database: Database) -> None:
    """A missing price is None, never zero."""
    metadata = MetaData()
    table = Table(
        "probe_null",
        metadata,
        Column("id", String(8), primary_key=True),
        Column("amount", MoneyColumn()),
    )
    metadata.create_all(database.engine)
    with database.engine.begin() as connection:
        connection.execute(insert(table).values(id="a", amount=None))
    with database.engine.connect() as connection:
        assert connection.execute(select(table.c.amount)).scalar_one() is None


def test_check_connection_reports_identity(database: Database) -> None:
    info = database.check_connection()
    assert info["user"] == "(sqlite)"
    assert "version" in info


def test_database_creates_no_schema(database: Database) -> None:
    """`Database` deliberately has no `create_all`. Schema comes only from
    migration files applied with an owner credential."""
    assert not hasattr(database, "create_all")
    assert not hasattr(database, "initialize")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://u:p@h/d", True),
        ("postgresql+psycopg://u:p@h/d", True),
        ("postgres://u:p@h/d", True),
        ("sqlite:///var/x.sqlite3", False),
    ],
)
def test_backend_detection(url: str, expected: bool) -> None:
    """Checked on the URL, not by constructing an engine: `create_engine`
    imports the DBAPI, and psycopg is an optional extra that a local
    developer is not required to have installed."""
    assert is_postgres_url(url) is expected
