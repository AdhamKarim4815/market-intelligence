"""Migrations: the rule that the application never changes the schema."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from akmarkets.db.engine import Database
from akmarkets.db.migrations import (
    Migration,
    MigrationError,
    apply_pending,
    load_migrations,
    schema_status,
    verify_schema,
)

CREATE_USERS = """
create table mi_users (
    user_id text primary key,
    email   text not null
);
--@postgres-only-begin
alter table mi_users enable row level security;
revoke all on mi_users from anon, authenticated;
--@postgres-only-end
"""


def test_postgres_only_blocks_are_stripped_for_sqlite() -> None:
    """RLS and GRANT have no SQLite equivalent, and the local backend must
    still run the same file rather than a hand-maintained second copy."""
    migration = Migration(version="0001", name="users", path=Path("x"), sql=CREATE_USERS)
    sqlite = " ".join(migration.statements_for("sqlite"))
    postgres = " ".join(migration.statements_for("postgresql"))

    assert "create table mi_users" in sqlite
    assert "row level security" not in sqlite
    assert "row level security" in postgres
    assert "revoke all" in postgres


def test_comment_lines_do_not_become_statements() -> None:
    migration = Migration(
        version="0001",
        name="x",
        path=Path("x"),
        sql="-- a comment\ncreate table a (id text);\n\n-- another\n",
    )
    assert migration.statements_for("sqlite") == ["create table a (id text)"]


def test_apply_then_verify(database: Database, settings: Any, write_migration: Any) -> None:
    write_migration("0001", "users", CREATE_USERS)
    applied = apply_pending(database.engine, settings.project_root)
    assert [m.version for m in applied] == ["0001"]

    status = verify_schema(database.engine, settings.project_root)
    assert status.ok
    assert status.current_version == "0001"

    with database.engine.connect() as connection:
        connection.execute(text("select user_id from mi_users")).all()


def test_apply_is_idempotent(database: Database, settings: Any, write_migration: Any) -> None:
    write_migration("0001", "users", CREATE_USERS)
    apply_pending(database.engine, settings.project_root)
    assert apply_pending(database.engine, settings.project_root) == []


def test_pending_migration_refuses_startup(
    database: Database, settings: Any, write_migration: Any
) -> None:
    """The code is newer than the database. Running anyway produces wrong
    numbers rather than an error, and a wrong portfolio value is
    indistinguishable from a right one until someone acts on it."""
    write_migration("0001", "users", CREATE_USERS)
    apply_pending(database.engine, settings.project_root)
    write_migration("0002", "later", "create table mi_later (id text primary key);")

    with pytest.raises(MigrationError, match="1 pending"):
        verify_schema(database.engine, settings.project_root)


def test_edited_migration_is_detected_separately_from_pending(
    database: Database, settings: Any, write_migration: Any
) -> None:
    """A file changed after it ran means the two describe different schemas,
    and no amount of re-running reconciles them. That needs a human, so it
    is reported as its own failure rather than as a pending migration."""
    write_migration("0001", "users", CREATE_USERS)
    apply_pending(database.engine, settings.project_root)
    write_migration("0001", "users", CREATE_USERS + "\n-- edited after the fact\n")

    status = schema_status(database.engine, settings.project_root)
    assert status.altered == ["0001_users"]
    assert status.pending == []
    with pytest.raises(MigrationError, match="edited after being applied"):
        verify_schema(database.engine, settings.project_root)


def test_a_failing_migration_does_not_record_itself(
    database: Database, settings: Any, write_migration: Any
) -> None:
    """One transaction per file: a failure leaves the ledger honest about
    where the database actually is."""
    write_migration("0001", "users", CREATE_USERS)
    write_migration("0002", "broken", "create table mi_broken (id text primary key;")

    with pytest.raises(OperationalError):
        apply_pending(database.engine, settings.project_root)

    status = schema_status(database.engine, settings.project_root)
    assert [record.version for record in status.applied] == ["0001"]
    assert [migration.version for migration in status.pending] == ["0002"]


def test_dry_run_applies_nothing(
    database: Database, settings: Any, write_migration: Any
) -> None:
    write_migration("0001", "users", CREATE_USERS)
    pending = apply_pending(database.engine, settings.project_root, dry_run=True)
    assert [m.version for m in pending] == ["0001"]
    assert schema_status(database.engine, settings.project_root).applied == []


def test_misnamed_file_is_an_error_not_a_skip(settings: Any) -> None:
    """A migration silently ignored because of a typo is the worst outcome
    available here."""
    (settings.project_root / "migrations" / "users.sql").write_text("select 1;")
    with pytest.raises(MigrationError, match="does not match"):
        load_migrations(settings.project_root)


def test_duplicate_version_is_rejected(settings: Any, write_migration: Any) -> None:
    write_migration("0001", "a", "create table a (id text);")
    write_migration("0001", "b", "create table b (id text);")
    with pytest.raises(MigrationError, match="duplicate"):
        load_migrations(settings.project_root)


def test_status_on_a_never_migrated_database(database: Database, settings: Any) -> None:
    """No ledger table yet is an empty history, not a crash."""
    status = schema_status(database.engine, settings.project_root)
    assert status.applied == []
    assert status.ok


DO_BLOCK = """
create table mi_thing (id text primary key);
--@postgres-only-begin
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'akmarkets_app') then
        create role akmarkets_app nologin;
    end if;
end
$$;
--@postgres-only-end
"""


def test_a_dollar_quoted_block_is_not_cut_at_its_inner_semicolons() -> None:
    """`end if;` ends a line and terminates nothing.

    Splitting there yields three fragments that each fail with a syntax
    error pointing nowhere near the real problem, which is how a role
    migration silently half-applies.
    """
    migration = Migration(version="0003", name="role", path=Path("x"), sql=DO_BLOCK)
    statements = migration.statements_for("postgresql")
    assert len(statements) == 2
    assert statements[0].startswith("create table mi_thing")
    assert statements[1].startswith("do $$")
    assert statements[1].endswith("$$")
    assert "end if;" in statements[1]


def test_a_semicolon_inside_a_string_literal_does_not_split() -> None:
    migration = Migration(
        version="0004",
        name="seed",
        path=Path("x"),
        sql="insert into mi_thing (id) values ('a;b');\nselect 1;",
    )
    assert migration.statements_for("postgresql") == [
        "insert into mi_thing (id) values ('a;b')",
        "select 1",
    ]


def test_a_sqlite_only_block_is_kept_on_sqlite_and_dropped_on_postgres() -> None:
    """Exact-decimal columns need different column *types* per backend:
    `numeric` in SQLite has NUMERIC affinity and would convert the stored
    decimal string back into a float."""
    sql = (
        "create table mi_t (id text);\n"
        "--@postgres-only-begin\n"
        "alter table mi_t add column price numeric(28,4);\n"
        "--@postgres-only-end\n"
        "--@sqlite-only-begin\n"
        "alter table mi_t add column price varchar(48);\n"
        "--@sqlite-only-end\n"
    )
    migration = Migration(version="0005", name="types", path=Path("x"), sql=sql)
    assert "numeric(28,4)" in " ".join(migration.statements_for("postgresql"))
    assert "varchar(48)" not in " ".join(migration.statements_for("postgresql"))
    assert "varchar(48)" in " ".join(migration.statements_for("sqlite"))
    assert "numeric(28,4)" not in " ".join(migration.statements_for("sqlite"))
