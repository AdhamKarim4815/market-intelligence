"""Portfolio pages and transaction entry.

Each case asserts on a fact the page exists to communicate rather than on a
status code — a template that renders an empty section still returns 200,
and that class of bug is easy to ship silently.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akmarkets.providers.base import Bar
from akmarkets.services import market_data
from akmarkets.web.app import create_app
from akmarkets.web.identity import ACCESS_EMAIL_HEADER

TODAY = date.today()


@pytest.fixture
def client(migrated: Any, settings: Any) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def priced(settings: Any) -> Any:
    """Seed the price cache so valuation has something to work with.

    Writing bars straight into the cache rather than stubbing a provider:
    the cache *is* the interface the web layer reads, and going through it
    exercises the same path production uses.
    """
    cache = market_data.build_price_cache(settings)

    def _seed(symbol: str, closes: dict[str, str]) -> None:
        cache.store(
            symbol,
            [
                Bar(
                    on=date.fromisoformat(day),
                    open=Decimal(price),
                    high=Decimal(price),
                    low=Decimal(price),
                    close=Decimal(price),
                    volume=1000,
                )
                for day, price in closes.items()
            ],
        )

    return _seed


def make_portfolio(client: TestClient, name: str = "Long Term") -> str:
    response = client.post("/portfolios", data={"name": name}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


# -- identity -----------------------------------------------------------


def test_the_access_header_identifies_the_user(client: TestClient) -> None:
    """Access supplies an email; everything downstream uses the internal id,
    so changing how people authenticate never invalidates their data."""
    body = client.get(
        "/portfolios", headers={ACCESS_EMAIL_HEADER: "Owner@Example.com"}
    ).text
    assert "owner@example.com" in body


def test_production_refuses_a_request_that_missed_the_perimeter(settings: Any) -> None:
    """No header in production means the request did not come through
    Access — a misconfigured DNS record, or the origin exposed directly.
    Serving it would hand someone a portfolio."""
    settings.env = "production"
    with TestClient(create_app(settings)) as production:
        assert production.get("/portfolios").status_code == 403


def test_two_users_do_not_see_each_others_portfolios(client: TestClient) -> None:
    client.post(
        "/portfolios",
        data={"name": "Mine"},
        headers={ACCESS_EMAIL_HEADER: "a@example.com"},
        follow_redirects=False,
    )
    other = client.get("/portfolios", headers={ACCESS_EMAIL_HEADER: "b@example.com"}).text
    assert "Mine" not in other
    assert "No portfolios yet" in other


def test_reading_someone_elses_portfolio_is_forbidden_not_missing(
    client: TestClient,
) -> None:
    """403 for a portfolio that exists but is not yours, 404 for one that
    does not exist. Collapsing both to 404 tells a caller less than they
    already know: the id came from somewhere."""
    response = client.post(
        "/portfolios",
        data={"name": "Private"},
        headers={ACCESS_EMAIL_HEADER: "a@example.com"},
        follow_redirects=False,
    )
    portfolio_id = response.headers["location"].rsplit("/", 1)[-1]

    forbidden = client.get(
        f"/portfolios/{portfolio_id}", headers={ACCESS_EMAIL_HEADER: "b@example.com"}
    )
    assert forbidden.status_code == 403
    assert client.get("/portfolios/does-not-exist").status_code == 404


# -- recording ----------------------------------------------------------


def test_a_recorded_buy_appears_as_a_position(client: TestClient, priced: Any) -> None:
    priced("ACME", {TODAY.isoformat(): "12"})
    portfolio_id = make_portfolio(client)
    client.post(
        f"/portfolios/{portfolio_id}/transactions",
        data={
            "transaction_type": "buy",
            "occurred_on": "2026-01-05",
            "symbol": "acme",
            "quantity": "100",
            "price": "10",
        },
        follow_redirects=False,
    )
    body = client.get(f"/portfolios/{portfolio_id}").text
    assert "ACME" in body
    assert "100" in body


def test_recording_redirects_so_a_refresh_does_not_repost(client: TestClient) -> None:
    """A dashboard that re-submits a trade on refresh is a portfolio that
    grows phantom positions."""
    portfolio_id = make_portfolio(client)
    response = client.post(
        f"/portfolios/{portfolio_id}/transactions",
        data={
            "transaction_type": "deposit",
            "occurred_on": "2026-01-05",
            "amount": "1000",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/portfolios/{portfolio_id}"


def test_a_position_transaction_without_a_symbol_is_rejected(client: TestClient) -> None:
    portfolio_id = make_portfolio(client)
    response = client.post(
        f"/portfolios/{portfolio_id}/transactions",
        data={"transaction_type": "buy", "occurred_on": "2026-01-05", "quantity": "10"},
    )
    assert response.status_code == 400
    assert "needs a symbol" in response.text


def test_a_negative_quantity_is_rejected_by_the_domain_not_the_form(
    client: TestClient,
) -> None:
    """Direction comes from the type, never a sign — and the rule is
    enforced in one place, so the form, the CLI and a future importer all
    reject it identically."""
    portfolio_id = make_portfolio(client)
    response = client.post(
        f"/portfolios/{portfolio_id}/transactions",
        data={
            "transaction_type": "buy",
            "occurred_on": "2026-01-05",
            "symbol": "ACME",
            "quantity": "-10",
            "price": "10",
        },
    )
    assert response.status_code == 400
    assert "magnitude" in response.text


def test_an_unparseable_date_is_rejected(client: TestClient) -> None:
    portfolio_id = make_portfolio(client)
    response = client.post(
        f"/portfolios/{portfolio_id}/transactions",
        data={"transaction_type": "deposit", "occurred_on": "yesterday", "amount": "10"},
    )
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.text


def test_deleting_a_transaction_from_another_portfolio_is_refused(
    client: TestClient,
) -> None:
    """The id is real but belongs elsewhere. Refusing on the mismatch stops
    a guessed id from deleting across a boundary the URL implied was
    checked."""
    first = make_portfolio(client, "One")
    second = make_portfolio(client, "Two")
    client.post(
        f"/portfolios/{first}/transactions",
        data={"transaction_type": "deposit", "occurred_on": "2026-01-05", "amount": "100"},
        follow_redirects=False,
    )
    body = client.get(f"/portfolios/{first}").text
    transaction_id = body.split(f"/portfolios/{first}/transactions/")[1].split("/delete")[0]

    wrong = client.post(f"/portfolios/{second}/transactions/{transaction_id}/delete")
    assert wrong.status_code == 404


# -- what the page communicates -----------------------------------------


def test_an_unpriced_holding_withholds_the_total_and_names_the_symbol(
    client: TestClient,
) -> None:
    """"3 positions unpriced" is not actionable; "XMPL unpriced" is."""
    portfolio_id = make_portfolio(client)
    client.post(
        f"/portfolios/{portfolio_id}/transactions",
        data={
            "transaction_type": "buy",
            "occurred_on": "2026-01-05",
            "symbol": "XMPL",
            "quantity": "100",
            "price": "10",
        },
        follow_redirects=False,
    )
    body = client.get(f"/portfolios/{portfolio_id}").text
    assert "Portfolio total unavailable" in body
    assert "XMPL" in body


def test_an_oversell_is_surfaced_on_the_page(client: TestClient) -> None:
    """A ledger that does not add up is shown, never silently corrected."""
    portfolio_id = make_portfolio(client)
    client.post(
        f"/portfolios/{portfolio_id}/transactions",
        data={
            "transaction_type": "sell",
            "occurred_on": "2026-01-05",
            "symbol": "ACME",
            "quantity": "50",
            "price": "20",
        },
        follow_redirects=False,
    )
    body = client.get(f"/portfolios/{portfolio_id}").text
    assert "issue(s) in this ledger" in body
    assert "only" in body


def test_an_empty_portfolio_says_so_rather_than_showing_zeroes(
    client: TestClient,
) -> None:
    portfolio_id = make_portfolio(client)
    body = client.get(f"/portfolios/{portfolio_id}").text
    assert "No open positions" in body


def test_the_period_switcher_offers_every_window(client: TestClient) -> None:
    portfolio_id = make_portfolio(client)
    body = client.get(f"/portfolios/{portfolio_id}").text
    for period in ("1d", "1w", "1m", "ytd", "1y", "all"):
        assert f"?period={period}" in body
