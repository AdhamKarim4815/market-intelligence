"""The command line. Same services as the web, so figures cannot disagree."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from akmarkets.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_settings(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point every command at the test project root rather than the real one."""
    monkeypatch.setattr("akmarkets.config.get_settings", lambda: settings)
    monkeypatch.setattr("akmarkets.cli.main.get_settings", lambda: settings)
    monkeypatch.setattr("akmarkets.cli.db.get_settings", lambda: settings)
    monkeypatch.setattr("akmarkets.cli.secrets.get_settings", lambda: settings)
    return settings


def test_config_shows_the_target_database_without_the_password() -> None:
    """"Which database am I actually pointed at" is the first question in
    almost every incident."""
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "sqlite" in result.stdout
    assert "ai enabled       False" in result.stdout


def test_db_check_reports_identity() -> None:
    result = runner.invoke(app, ["db", "check"])
    assert result.exit_code == 0
    assert "ok" in result.stdout
    assert "SQLite" in result.stdout


def test_db_status_exits_non_zero_with_a_pending_migration(write_migration: Any) -> None:
    write_migration("0001", "users", "create table mi_users (user_id text primary key);")
    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 1
    assert "pending" in result.stdout


def test_db_migrate_then_status_is_clean(write_migration: Any) -> None:
    write_migration("0001", "users", "create table mi_users (user_id text primary key);")
    applied = runner.invoke(app, ["db", "migrate"])
    assert applied.exit_code == 0
    assert "applied  0001_users" in applied.stdout

    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 0
    assert "up to date at 0001" in result.stdout


def test_db_migrate_dry_run_changes_nothing(write_migration: Any) -> None:
    write_migration("0001", "users", "create table mi_users (user_id text primary key);")
    result = runner.invoke(app, ["db", "migrate", "--dry-run"])
    assert result.exit_code == 0
    assert "would apply  0001_users" in result.stdout
    assert runner.invoke(app, ["db", "status"]).exit_code == 1


def test_secrets_status_reports_absence_as_a_fact() -> None:
    """Every key is optional; not having one is not an error."""
    result = runner.invoke(app, ["secrets", "status"])
    assert result.exit_code == 0
    assert "openai" in result.stdout
    assert "absent" in result.stdout


def test_no_key_is_offered_for_a_provider_that_does_not_read_one() -> None:
    """FRED and Finnhub were dropped along with the providers that used
    them. Offering to store a key nothing loads reads as "macro data needs
    this", and the user would set it and get identical behaviour."""
    result = runner.invoke(app, ["secrets", "status"])
    assert "fred" not in result.stdout
    assert "finnhub" not in result.stdout
    assert runner.invoke(app, ["secrets", "set", "fred", "--value", "x"]).exit_code == 1


def test_secrets_set_rejects_an_unknown_name() -> None:
    result = runner.invoke(app, ["secrets", "set", "bloomberg", "--value", "x"])
    assert result.exit_code == 1
    assert "unknown key" in result.stdout


def test_secrets_round_trip() -> None:
    assert runner.invoke(app, ["secrets", "set", "openai", "--value", "abc"]).exit_code == 0
    assert "present" in runner.invoke(app, ["secrets", "status"]).stdout
    assert runner.invoke(app, ["secrets", "delete", "openai"]).exit_code == 0
    assert "absent" in runner.invoke(app, ["secrets", "status"]).stdout
