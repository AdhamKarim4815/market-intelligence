"""Shared fixtures.

Two things matter here. Tests must never touch the real macOS keychain, and
they must never touch a real database or the developer's `var/` directory.
Both are enforced by autouse fixtures rather than by remembering.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.db.migrations import apply_pending


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """An in-memory stand-in for the system keychain.

    Autouse because a test that reaches the real keychain either prompts for
    a password on macOS or silently reads the developer's actual API keys,
    and both are unacceptable in a test run.
    """
    store: dict[tuple[str, str], str] = {}

    def get_password(service: str, account: str) -> str | None:
        return store.get((service, account))

    def set_password(service: str, account: str, password: str) -> None:
        store[(service, account)] = password

    def delete_password(service: str, account: str) -> None:
        store.pop((service, account), None)

    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)
    monkeypatch.setattr("keyring.get_keyring", lambda: object())
    return store


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """An isolated project root, with an empty migrations directory."""
    (tmp_path / "migrations").mkdir()
    (tmp_path / "var").mkdir()
    return tmp_path


@pytest.fixture
def settings(project_root: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings pointed entirely inside tmp_path.

    `project_root` is patched on the class rather than passed around,
    because it is a property derived from the module's own location and
    everything that writes to disk reads it.
    """
    monkeypatch.setattr(
        Settings, "project_root", property(lambda _self: project_root), raising=False
    )
    return Settings(
        env="test",
        database_url=f"sqlite:///{project_root / 'var' / 'test.sqlite3'}",
    )


@pytest.fixture
def database(settings: Settings) -> Iterator[Database]:
    db = Database(settings.database_url)
    yield db
    db.dispose()


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def real_migrations(settings: Settings) -> Settings:
    """Copy the repository's actual migrations into the isolated root.

    Tests that need a schema build it from the files that ship, not from
    `Base.metadata.create_all`. A model and a migration drifting apart is
    exactly the failure this project's schema discipline exists to prevent,
    so the tests have to exercise the real files or they would not catch it.
    """
    target = settings.project_root / "migrations"
    for source in sorted((REPO_ROOT / "migrations").glob("*.sql")):
        (target / source.name).write_text(source.read_text(), encoding="utf-8")
    return settings


@pytest.fixture
def migrated(real_migrations: Settings, database: Database) -> Database:
    apply_pending(database.engine, real_migrations.project_root)
    return database


@pytest.fixture
def write_migration(project_root: Path) -> Any:
    """Drop a migration file into the isolated project root."""

    def _write(version: str, name: str, sql: str) -> Path:
        path = project_root / "migrations" / f"{version}_{name}.sql"
        path.write_text(sql, encoding="utf-8")
        return path

    return _write
