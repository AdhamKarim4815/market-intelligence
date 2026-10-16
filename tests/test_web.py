"""Web surface.

Each case asserts on a fact the page exists to communicate rather than on a
status code. A template that renders an empty section still returns 200, and
that class of bug is easy to ship silently.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akmarkets.web.app import create_app


@pytest.fixture
def client(migrated: Any, settings: Any) -> Any:
    """The app against a database built by the real migrations.

    An app pointed at an empty database is not a healthy app, and the
    health check correctly says so -- so the tests that assert "healthy"
    have to give it a real schema.
    """
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_dashboard_shows_an_empty_state_rather_than_zeros(client: TestClient) -> None:
    """A placeholder number is indistinguishable from a real one.

    Replaces an assertion on milestone text ("No portfolio yet — M1") that
    survived long after both milestones shipped, and which passed happily
    against a page rendering three em dashes and nothing else.
    """
    body = client.get("/").text
    assert "Overview" in body
    assert "—" in body
    assert "Create one" in body
    assert "0.00" not in body


def test_dashboard_shows_the_portfolio_value_it_can_compute(
    client: TestClient, migrated: Any
) -> None:
    """The page exists to answer "what is it worth".

    Asserting on a status code passes against a template that renders three
    empty cards — which is precisely the bug this replaces.
    """
    from decimal import Decimal

    from akmarkets.domain.records import TransactionType
    from akmarkets.services import portfolio as portfolio_service

    with migrated.sessions.begin() as session:
        user = portfolio_service.get_or_create_user(session, "local@localhost")
        folio = portfolio_service.create_portfolio(session, user.user_id, "Long Term")
        portfolio_service.record_transaction(
            session,
            folio.portfolio_id,
            transaction_type=TransactionType.DEPOSIT,
            occurred_on=date(2026, 1, 2),
            amount=Decimal("1000"),
        )

    body = client.get("/").text
    assert "Long Term" in body
    assert "$1,000.00" in body


def test_the_nav_reaches_inbox_risk_markets_and_jobs(client: TestClient) -> None:
    """Three links and five rich pages meant inbox and risk were reachable
    only by knowing a URL, which is the same as not existing."""
    body = client.get("/").text
    for href in ('href="/inbox"', 'href="/risk"', 'href="/markets"', 'href="/jobs"'):
        assert href in body


def test_health_page_lists_each_component_with_a_state(client: TestClient) -> None:
    body = client.get("/health").text
    assert "database" in body
    assert "schema" in body
    assert "state-healthy" in body


def test_health_api_returns_503_when_something_is_wrong(settings: Any) -> None:
    """A monitor that only ever gets 200 is not a monitor.

    The database URL is pointed at a directory, which connects the same way
    a wrong path or a revoked credential would: the engine is constructed
    fine and the failure appears on first use.
    """
    settings.database_url = f"sqlite:///{settings.project_root / 'var'}"
    with TestClient(create_app(settings)) as broken:
        response = broken.get("/api/health")
    assert response.status_code == 503
    payload = response.json()
    assert payload["state"] == "failed"
    # The schema check cannot run without a database, and says so rather
    # than reporting healthy by default.
    states = {component["name"]: component["state"] for component in payload["components"]}
    assert states == {"database": "failed", "schema": "unknown"}


def test_health_api_is_200_and_healthy_normally(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "healthy"
    assert {component["name"] for component in payload["components"]} == {"database", "schema"}


def test_status_endpoint_is_idle_before_any_job_runs(client: TestClient) -> None:
    payload = client.get("/api/status").json()
    assert payload["busy"] is False


def test_activity_endpoint_resumes_from_a_cursor(client: TestClient) -> None:
    from akmarkets.activity import ACTIVITY

    first = ACTIVITY.emit("test", "alpha")
    ACTIVITY.emit("test", "beta")
    payload = client.get(f"/api/activity?since={first.id}").json()
    assert [event["message"] for event in payload["events"]] == ["beta"]


def test_pending_migration_is_surfaced_on_the_page_not_swallowed(
    settings: Any, write_migration: Any
) -> None:
    """Startup refuses to treat an unmigrated database as normal, and says
    so where a person will see it."""
    write_migration("0001", "users", "create table mi_users (user_id text primary key);")
    with TestClient(create_app(settings)) as unmigrated:
        body = unmigrated.get("/").text
    assert "Schema mismatch" in body
    assert "1 pending migration" in body


def test_no_openapi_or_docs_surface_is_exposed(client: TestClient) -> None:
    """The application is private and behind Cloudflare Access; an
    unauthenticated schema dump is not something it needs to offer."""
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
