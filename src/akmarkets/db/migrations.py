"""Version-controlled schema migrations.

The rule this module exists to enforce: **the application never changes the
schema.** Startup verifies and refuses; it does not create, alter, drop or
repair. Schema changes are files in `migrations/`, reviewed in the
repository, and applied deliberately with an owner credential. The runtime
role `akmarkets_app` has no DDL privileges at all, so this is enforced by
the database as well as by convention -- the belt and the braces disagree
about nothing.

That is a deliberate departure from the common pattern of calling
`Base.metadata.create_all()` on startup. `create_all` silently does nothing
when a table exists but its columns have drifted, so it appears to work
right up until a query fails against production, and it can only ever add.

**Dialect portability.** These files run against both Postgres (production)
and SQLite (local development, which must need no cloud credentials). Most
DDL is portable if written plainly. The parts that are not are wrapped in
guarded blocks and stripped before execution on the other backend:

    --@postgres-only-begin
    alter table mi_users enable row level security;
    --@postgres-only-end

    --@sqlite-only-begin
    alter table mi_transactions add column price varchar(48) not null default '0';
    --@sqlite-only-end

Two things genuinely need this. Roles, GRANT and RLS have no SQLite
equivalent at all. And exact-decimal columns need different *types*: a
column declared `numeric` in SQLite has NUMERIC affinity, which silently
converts the decimal string we store back into a float and defeats the
entire point of `akmarkets.db.types`. Those columns are therefore declared
`numeric` on Postgres and `varchar` on SQLite, which is what the
`ExactDecimal` type already expects on each side.

Blocks are for syntax that differs, never for behaviour that differs. A
migration whose two branches would produce different *data* is a bug, not a
portability measure.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

MIGRATIONS_TABLE = "mi_schema_migrations"

_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


def _block(dialect: str) -> re.Pattern[str]:
    return re.compile(
        rf"--@{dialect}-only-begin.*?--@{dialect}-only-end",
        re.DOTALL | re.IGNORECASE,
    )


_BLOCKS = {"postgres": _block("postgres"), "sqlite": _block("sqlite")}

# `$$`, `$body$`, `$fn$` — Postgres dollar quoting, where everything up to
# the matching tag is literal text including semicolons.
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _split_statements(sql: str) -> list[str]:
    """Split SQL on top-level semicolons.

    Semicolons inside single-quoted strings, line comments and dollar-quoted
    bodies terminate nothing. A `do $$ ... end if; ... $$` block is the case
    that actually arises here — cutting it produces fragments that each fail
    with a syntax error pointing nowhere near the real problem.
    """
    statements: list[str] = []
    start = 0
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'":
                    # Doubled quote is an escaped quote, not a terminator.
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    break
                index += 1
            index += 1
            continue

        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue

        if char == "$":
            match = _DOLLAR_TAG.match(sql, index)
            if match:
                tag = match.group(0)
                closing = sql.find(tag, match.end())
                index = length if closing == -1 else closing + len(tag)
                continue

        if char == ";":
            statements.append(sql[start:index])
            index += 1
            start = index
            continue

        index += 1

    statements.append(sql[start:])
    return statements


def _strip_comments(chunk: str) -> str:
    return "\n".join(
        line for line in chunk.splitlines() if not line.strip().startswith("--")
    ).strip()


class MigrationError(RuntimeError):
    """The schema on disk and the schema in the database do not agree."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def statements_for(self, dialect: str) -> list[str]:
        """Split into statements, dropping blocks the dialect cannot run.

        Splitting on semicolons sounds trivial and is not: a `do $$ ... $$`
        block contains semicolons that terminate nothing, and cutting them
        produces fragments that each fail with a syntax error far from the
        real cause. Splitting only on semicolons at end of line does not
        help either — `end if;` ends a line too. So this tracks quoting
        state properly and splits on top-level semicolons only.
        """
        # SQLAlchemy reports Postgres as "postgresql"; the block marker is
        # spelled "postgres" because that is what anyone writing the file
        # will type.
        keep = "sqlite" if dialect == "sqlite" else "postgres"
        body = self.sql
        for name, pattern in _BLOCKS.items():
            if name != keep:
                body = pattern.sub("", body)
        return [
            cleaned
            for chunk in _split_statements(body)
            if (cleaned := _strip_comments(chunk))
        ]


@dataclass(frozen=True)
class AppliedMigration:
    version: str
    name: str
    checksum: str
    applied_at: datetime | None


def migrations_dir(project_root: Path) -> Path:
    return project_root / "migrations"


def load_migrations(project_root: Path) -> list[Migration]:
    """Every migration file on disk, in version order.

    A filename that does not match `NNNN_lower_snake.sql` is an error rather
    than a skip. A migration silently ignored because of a typo in its name
    is the worst possible outcome here.
    """
    directory = migrations_dir(project_root)
    if not directory.exists():
        return []
    found: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.is_dir():
            continue
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"migration filename {path.name!r} does not match NNNN_lower_snake_case.sql"
            )
        found.append(
            Migration(
                version=match.group("version"),
                name=match.group("name"),
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )
    versions = [migration.version for migration in found]
    duplicates = {version for version in versions if versions.count(version) > 1}
    if duplicates:
        raise MigrationError(f"duplicate migration version(s): {sorted(duplicates)}")
    return found


def ensure_migrations_table(engine: Engine) -> None:
    """Create the ledger table itself. The only DDL outside a migration file.

    It has to be, since it is what records that a migration file ran. Called
    only by the migrate command, which uses an owner credential.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                create table if not exists {MIGRATIONS_TABLE} (
                    version     text primary key,
                    name        text not null,
                    checksum    text not null,
                    applied_at  timestamp not null
                )
                """
            )
        )


def applied_migrations(engine: Engine) -> list[AppliedMigration]:
    """What the database says has been applied, or [] if never migrated."""
    with engine.connect() as connection:
        exists = connection.dialect.has_table(connection, MIGRATIONS_TABLE)
        if not exists:
            return []
        rows = connection.execute(
            text(
                f"select version, name, checksum, applied_at "
                f"from {MIGRATIONS_TABLE} order by version"
            )
        ).all()
    return [
        AppliedMigration(
            version=str(row[0]),
            name=str(row[1]),
            checksum=str(row[2]),
            applied_at=row[3] if isinstance(row[3], datetime) else None,
        )
        for row in rows
    ]


@dataclass(frozen=True)
class SchemaStatus:
    applied: list[AppliedMigration]
    pending: list[Migration]
    altered: list[str]

    @property
    def ok(self) -> bool:
        return not self.pending and not self.altered

    @property
    def current_version(self) -> str | None:
        return self.applied[-1].version if self.applied else None

    def describe(self) -> str:
        if self.altered:
            return (
                f"schema mismatch: migration(s) {', '.join(self.altered)} were edited "
                f"after being applied"
            )
        if self.pending:
            names = ", ".join(f"{m.version}_{m.name}" for m in self.pending)
            return f"{len(self.pending)} pending migration(s): {names}"
        return f"schema up to date at {self.current_version or '(empty)'}"


def schema_status(engine: Engine, project_root: Path) -> SchemaStatus:
    """Compare the files on disk against the ledger in the database.

    Detects two different failures. *Pending* means the code is newer than
    the database, which is the ordinary deploy-ordering problem. *Altered*
    means a migration file changed after it ran, which means the two are
    describing different schemas and no amount of re-running will reconcile
    them -- that needs a human, so it is reported separately.
    """
    on_disk = load_migrations(project_root)
    in_db = {record.version: record for record in applied_migrations(engine)}
    pending = [migration for migration in on_disk if migration.version not in in_db]
    altered = [
        f"{migration.version}_{migration.name}"
        for migration in on_disk
        if migration.version in in_db and in_db[migration.version].checksum != migration.checksum
    ]
    return SchemaStatus(applied=list(in_db.values()), pending=pending, altered=altered)


def verify_schema(engine: Engine, project_root: Path) -> SchemaStatus:
    """Called at startup. Raises rather than starting on an incompatible schema.

    Refusing to start is the point. An application that runs against a
    schema it was not written for produces wrong numbers rather than an
    error, and wrong numbers in a portfolio are indistinguishable from right
    ones until someone acts on them.
    """
    status = schema_status(engine, project_root)
    if not status.ok:
        raise MigrationError(status.describe())
    return status


def apply_pending(engine: Engine, project_root: Path, dry_run: bool = False) -> list[Migration]:
    """Apply every pending migration, each in its own transaction.

    One transaction per file rather than one for the batch: if migration
    three fails, one and two are durably applied and recorded, and the
    ledger tells the truth about where the database actually is. A single
    wrapping transaction would roll all three back and lose that.
    """
    ensure_migrations_table(engine)
    status = schema_status(engine, project_root)
    if status.altered:
        raise MigrationError(status.describe())
    if dry_run:
        return status.pending

    dialect = engine.dialect.name
    applied: list[Migration] = []
    for migration in status.pending:
        logger.info("applying migration %s_%s", migration.version, migration.name)
        with engine.begin() as connection:
            for statement in migration.statements_for(dialect):
                connection.execute(text(statement))
            connection.execute(
                text(
                    f"insert into {MIGRATIONS_TABLE} (version, name, checksum, applied_at) "
                    f"values (:version, :name, :checksum, :applied_at)"
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                    "applied_at": datetime.now().astimezone(),
                },
            )
        applied.append(migration)
    return applied
