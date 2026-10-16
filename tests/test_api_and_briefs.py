"""The v1 API contract, and briefs that leave things out."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from akmarkets.db.engine import Database
from akmarkets.domain.market import Bar
from akmarkets.domain.news import Severity
from akmarkets.services import briefs, market_data
from akmarkets.services import portfolio as portfolio_service
from akmarkets.services.identity import ACCESS_EMAIL_HEADER
from akmarkets.web.app import create_app

TODAY = date.today()


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


@pytest.fixture
def seeded(migrated: Database, settings: Any) -> str:
    """Seed in its own committed transaction.

    Holding a write transaction open while the app opens a second
    connection deadlocks SQLite — which is a fixture bug, not a product
    one, but it looks exactly like a product one.
    """
    from akmarkets.domain.records import TransactionType

    with migrated.sessions.begin() as setup:
        user = portfolio_service.get_or_create_user(setup, "local@localhost")
        folio = portfolio_service.create_portfolio(setup, user.user_id, "Long Term")
        acme = portfolio_service.get_or_create_instrument(setup, "ACME", name="Acme")
        portfolio_service.record_transaction(
            setup, folio.portfolio_id, TransactionType.DEPOSIT,
            date(2026, 1, 5), amount=10_000,
        )
        portfolio_service.record_transaction(
            setup, folio.portfolio_id, TransactionType.BUY, date(2026, 1, 5),
            instrument_id=acme.instrument_id, quantity=100, price=50,
        )
        portfolio_id = folio.portfolio_id

    cache = market_data.build_price_cache(settings)
    cache.store(
        "ACME",
        [
            Bar(
                on=TODAY - timedelta(days=n), open=Decimal(60), high=Decimal(60),
                low=Decimal(60), close=Decimal(60), volume=1000,
            )
            for n in range(90, -1, -1)
        ],
    )
    return portfolio_id


@pytest.fixture
def client(migrated: Database, settings: Any) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# -- the contract -------------------------------------------------------


def test_every_response_carries_freshness_on_the_envelope(
    seeded: str, client: TestClient
) -> None:
    """A client that has been offline needs to know whether what it is
    showing is worth showing, before the user reads it."""
    payload = client.get(f"/api/v1/portfolios/{seeded}").json()
    assert payload["api_version"] == "1.0"
    assert "as_of" in payload
    assert "stale" in payload
    assert "retrieved_at" in payload
    assert "data" in payload


def test_money_is_a_string_never_a_json_number(seeded: str, client: TestClient) -> None:
    """A JSON number is a float in every client that parses it, and the
    whole stack keeps money exact so it does not become one at the last
    step."""
    data = client.get(f"/api/v1/portfolios/{seeded}").json()["data"]
    assert isinstance(data["total_value"], str)
    # Deposited 10,000 and bought 100 ACME at 50, so 5,000 cash remains and
    # the holding is worth 100 × 60 = 6,000.
    assert Decimal(data["total_value"]) == Decimal("11000")
    assert isinstance(data["positions"][0]["market_value"], str)


def test_an_unknown_total_is_null_and_says_why(
    migrated: Database, client: TestClient, settings: Any
) -> None:
    """Never 0 — a client renders zero as a real number and the user
    believes it."""
    from akmarkets.domain.records import TransactionType

    with migrated.sessions.begin() as setup:
        user = portfolio_service.get_or_create_user(setup, "local@localhost")
        folio = portfolio_service.create_portfolio(setup, user.user_id, "Unpriced")
        nope = portfolio_service.get_or_create_instrument(setup, "NOPRICE")
        portfolio_service.record_transaction(
            setup, folio.portfolio_id, TransactionType.BUY, date(2026, 1, 5),
            instrument_id=nope.instrument_id, quantity=10, price=5,
        )
        portfolio_id = folio.portfolio_id

    data = client.get(f"/api/v1/portfolios/{portfolio_id}").json()["data"]
    assert data["total_value"] is None
    assert data["incomplete"] is True
    assert data["unpriced_symbols"] == ["NOPRICE"]


def test_lists_are_paginated_from_the_first_release(
    seeded: str, client: TestClient
) -> None:
    """Adding pagination later is a breaking change; doing it now costs one
    field."""
    payload = client.get(f"/api/v1/portfolios/{seeded}/alerts?limit=5").json()["data"]
    assert payload["limit"] == 5
    assert payload["offset"] == 0
    assert "has_more" in payload


def test_alerts_carry_a_deep_link_and_a_stable_id(
    seeded: str, client: TestClient
) -> None:
    payload = client.get(f"/api/v1/portfolios/{seeded}/alerts").json()["data"]
    assert "unread_counts" in payload
    for alert in payload["alerts"]:
        assert alert["deep_link"].startswith(f"/portfolios/{seeded}/inbox#")


def test_valuation_ships_both_not_applicable_and_unavailable(
    seeded: str, client: TestClient
) -> None:
    """One is a statement about the company, the other a gap in our data,
    and a client that conflates them tells its user nothing."""
    payload = client.get("/api/v1/securities/ACME").json()["data"]
    assert payload["symbol"] == "ACME"
    # No fundamentals cached, so valuation is absent entirely rather than
    # a set of nulls that look like measurements.
    assert payload["valuation"] is None
    assert payload["technical"] is not None


def test_another_users_portfolio_is_forbidden_over_the_api(
    seeded: str, client: TestClient
) -> None:
    response = client.get(
        f"/api/v1/portfolios/{seeded}", headers={ACCESS_EMAIL_HEADER: "other@example.com"}
    )
    assert response.status_code == 403


def test_production_refuses_an_api_request_that_missed_the_perimeter(
    settings: Any, migrated: Database
) -> None:
    settings.env = "production"
    with TestClient(create_app(settings)) as production:
        assert production.get("/api/v1/portfolios").status_code == 403


def test_risk_names_what_it_could_not_measure(seeded: str, client: TestClient) -> None:
    """A client must not be able to present a partial figure as a whole
    one."""
    data = client.get(f"/api/v1/portfolios/{seeded}/risk").json()["data"]
    assert "insufficient_history" in data
    assert isinstance(data["insufficient_history"], list)


# -- briefs -------------------------------------------------------------


def test_an_empty_section_is_omitted_rather_than_rendered_empty(
    migrated: Database, settings: Any, seeded: str
) -> None:
    """A section saying "we looked and there was nothing" costs a reader the
    same attention as one that matters."""
    with migrated.sessions.begin() as read:
        brief = briefs.build_brief(read, settings, seeded)
    assert all(not section.is_empty for section in brief.sections)
    assert "Detractors" not in {section.title for section in brief.sections}


def test_a_quiet_brief_says_so_in_one_line(session: Session, settings: Any) -> None:
    user = portfolio_service.get_or_create_user(session, "local@localhost")
    folio = portfolio_service.create_portfolio(session, user.user_id, "Empty")
    brief = briefs.build_brief(session, settings, folio.portfolio_id)
    assert brief.is_quiet is True
    assert "Nothing needs your attention" in brief.render()


def test_the_headline_counts_what_deserves_attention(
    migrated: Database, settings: Any, seeded: str
) -> None:
    with migrated.sessions.begin() as read:
        brief = briefs.build_brief(read, settings, seeded)
    assert "attention" in brief.headline
    # The value line is always present, and is context rather than news.
    assert brief.summary


# -- the router ---------------------------------------------------------


def test_the_inbox_receives_everything_regardless_of_the_threshold() -> None:
    """It is the durable record, and a user who lowers their push threshold
    later should still find the history there."""
    delivered: list[briefs.Channel] = []
    router = briefs.Router()
    router.register(briefs.Channel.INBOX, lambda n: delivered.append(briefs.Channel.INBOX) or True)  # type: ignore[func-returns-value]
    router.register(briefs.Channel.EMAIL, lambda n: delivered.append(briefs.Channel.EMAIL) or True)  # type: ignore[func-returns-value]

    quiet = briefs.Notification(
        portfolio_id="p", severity=Severity.WATCH, title="minor", body=""
    )
    router.send(quiet, minimum=Severity.IMPORTANT)
    assert delivered == [briefs.Channel.INBOX]


def test_one_broken_channel_does_not_stop_the_others() -> None:
    """A broken email configuration must not prevent the inbox — the channel
    that always works — from receiving anything."""

    def explode(notification: briefs.Notification) -> bool:
        raise RuntimeError("smtp is down")

    router = briefs.Router()
    router.register(briefs.Channel.EMAIL, explode)
    router.register(briefs.Channel.INBOX, lambda n: True)

    results = router.send(
        briefs.Notification(
            portfolio_id="p", severity=Severity.CRITICAL, title="x", body=""
        )
    )
    assert results[briefs.Channel.EMAIL] is False
    assert results[briefs.Channel.INBOX] is True


def test_a_notification_carries_a_deep_link_and_no_formatting() -> None:
    """Channel-independent by construction: adding iOS push later means
    writing one function, not touching what decided this was worth
    sending."""
    notification = briefs.Notification(
        portfolio_id="p1", severity=Severity.CRITICAL, title="x", body="y",
        subject_id="a1",
    )
    assert notification.deep_link == "/portfolios/p1/inbox#a1"


def test_the_as_of_is_the_valuation_date_not_the_last_trade(
    seeded: str, client: TestClient
) -> None:
    """`state.as_of` falls back to the last transaction when no date is
    given, so a portfolio last traded in January would report January and be
    marked stale while being valued at today's close — the opposite of what
    the field is for."""
    from akmarkets.domain.sessions import last_completed_session

    payload = client.get(f"/api/v1/portfolios/{seeded}").json()
    # The last completed session, not today: a portfolio valued against a
    # session still in progress would show a number that will still change.
    assert payload["as_of"] == last_completed_session().isoformat()
    assert payload["stale"] is False
