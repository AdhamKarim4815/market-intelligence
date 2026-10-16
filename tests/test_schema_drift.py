"""The ORM models and the migration files must describe the same schema.

Nothing else catches this. `Base.metadata` is deliberately never used to
create anything, so a column added to a model without a matching migration
would pass every other test in the suite and then fail against production on
the first query that touched it.
"""

from __future__ import annotations

from sqlalchemy import inspect

from akmarkets.db.base import Base
from akmarkets.db.engine import Database


def _model_tables() -> dict[str, set[str]]:
    return {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.tables.values()
    }


def test_every_model_table_exists_in_the_migrations(migrated: Database) -> None:
    present = set(inspect(migrated.engine).get_table_names())
    missing = sorted(set(_model_tables()) - present)
    assert not missing, f"models declare tables no migration creates: {missing}"


def test_every_model_column_exists_in_the_migrations(migrated: Database) -> None:
    inspector = inspect(migrated.engine)
    problems: list[str] = []
    for table, expected in _model_tables().items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        for column in sorted(expected - actual):
            problems.append(f"{table}.{column} is on the model but not in the schema")
        for column in sorted(actual - expected):
            problems.append(f"{table}.{column} is in the schema but not on the model")
    assert not problems, problems


def test_every_table_is_namespaced(migrated: Database) -> None:
    """The `mi_` prefix is a security boundary, not a naming preference: the
    runtime role is granted on that namespace and nothing else, so a table
    created outside it would be unreachable in production."""
    stray = [
        name
        for name in inspect(migrated.engine).get_table_names()
        if not name.startswith("mi_")
    ]
    assert not stray, f"tables outside the mi_ namespace: {stray}"


def test_decimal_columns_are_text_typed_on_sqlite(migrated: Database) -> None:
    """A column declared `numeric` in SQLite has NUMERIC affinity, which
    converts the exact decimal string we store back into a float. That would
    make the local backend silently less accurate than production."""
    columns = {
        column["name"]: str(column["type"]).upper()
        for column in inspect(migrated.engine).get_columns("mi_transactions")
    }
    for name in ("quantity", "price", "fees", "amount", "split_ratio"):
        assert "CHAR" in columns[name], (
            f"mi_transactions.{name} is {columns[name]} on SQLite; it must have "
            f"TEXT affinity or stored decimals are coerced to float"
        )
