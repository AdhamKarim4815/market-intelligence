"""Who is allowed to see a page.

Before the overview carried data it resolved no identity at all, so a
request that reached the origin directly was served the dashboard. That was
harmless while the page was three em dashes and is not harmless now.

`services.identity` trusts the Cloudflare email header unconditionally —
"the header is trusted only because the perimeter is". These tests hold the
other half of that bargain: in production, no header means no page.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from akmarkets.web.app import create_app

OWNER = {"cf-access-authenticated-user-email": "you@example.com"}

# Pages that carry no user data and exist to report that the application is
# alive. `/api/health` in particular is the monitor endpoint: putting it
# behind identity would mean an outage and a misconfiguration look the same.
PUBLIC = {"/health", "/api/health", "/api/status", "/api/activity", "/api/activity/stream"}


@pytest.fixture
def production(migrated: Any, settings: Any) -> Any:
    settings.env = "production"
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def development(migrated: Any, settings: Any) -> Any:
    with TestClient(create_app(settings)) as client:
        yield client


def test_production_refuses_the_overview_without_an_access_header(
    production: TestClient,
) -> None:
    """The regression guard for making `/` data-bearing.

    A direct hit on the origin — bypassing Cloudflare — must not be served
    a portfolio. 403, not a friendly empty page.
    """
    assert production.get("/").status_code == 403


@pytest.mark.parametrize("path", ["/", "/portfolios", "/markets", "/jobs", "/inbox", "/risk"])
def test_production_refuses_every_data_bearing_page(
    production: TestClient, path: str
) -> None:
    """One protected route and five unprotected ones is not a perimeter."""
    assert production.get(path, follow_redirects=False).status_code == 403


def test_development_falls_back_to_the_local_developer(
    development: TestClient,
) -> None:
    """A laptop has no Cloudflare in front of it, and requiring one would
    make the application undevelopable."""
    response = development.get("/")
    assert response.status_code == 200
    assert "local@localhost" in response.text


def test_the_signed_in_identity_is_shown_on_the_page(
    development: TestClient,
) -> None:
    """Printing it is a control, not decoration: behind a perimeter, being
    authenticated as the wrong person is otherwise completely silent."""
    body = development.get("/", headers=OWNER).text
    assert "you@example.com" in body
    assert "cloudflare-access" in body


def test_every_html_page_resolves_an_identity(migrated: Any, settings: Any) -> None:
    """The next route someone adds should inherit the perimeter rather than
    quietly opt out of it.

    Walks the real route table instead of a hand-kept list, because a
    hand-kept list is exactly what fails to mention the route that was
    added last week.
    """
    application = create_app(settings)
    unguarded: list[str] = []

    for route in application.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if route.path in PUBLIC or route.path.startswith("/api/v1"):
            continue
        module = __import__(route.endpoint.__module__, fromlist=["*"])
        source = getattr(module, "__file__", None)
        if source is None:
            continue
        with open(source) as handle:
            if "resolve_user" not in handle.read():
                unguarded.append(f"{route.path} ({route.endpoint.__module__})")

    assert not unguarded, f"routes that never resolve an identity: {unguarded}"
