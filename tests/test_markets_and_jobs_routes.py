"""The two pages that gave rates, catalysts and job history a home.

All three were collected and stored by the pipeline while being readable
only from a terminal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akmarkets.db.models import EarningsEventRow, JobRunRow, MacroObservationRow
from akmarkets.web.app import create_app


@pytest.fixture
def client(migrated: Any, settings: Any) -> Any:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def observation(series_id: str, day: date, value: str, name: str) -> MacroObservationRow:
    return MacroObservationRow(
        series_id=series_id,
        observed_on=day,
        value=Decimal(value),
        unit="percent",
        name=name,
        source="us-treasury",
        retrieved_at=datetime.now(UTC),
    )


def test_a_rate_always_carries_its_unit_and_two_places(
    client: TestClient, migrated: Any
) -> None:
    """A rate and a change in a rate are both small numbers, and confusing
    one for the other is silent. 4.20 must not print as "4.2" beside 4.65
    in the same column."""
    with migrated.sessions.begin() as session:
        session.add(observation("BC_10YEAR", date(2026, 8, 3), "4.20", "10-year"))
        session.add(observation("BC_2YEAR", date(2026, 8, 3), "4.65", "2-year"))

    body = client.get("/markets").text
    assert "4.20%" in body
    assert "4.65%" in body


def test_the_markets_page_interprets_nothing(client: TestClient, migrated: Any) -> None:
    """A curve-shape call is exactly the kind of figure this project
    forbids — one with no methodology behind it. The page prints published
    yields and arithmetic differences, and stops there.

    The explanatory notes are stripped before scanning, because the note
    that says the page does not call a regime necessarily contains the word
    "regime". What is asserted is that no interpretation appears in the
    data itself.
    """
    import re

    with migrated.sessions.begin() as session:
        session.add(observation("BC_10YEAR", date(2026, 8, 3), "4.20", "10-year"))
        session.add(observation("BC_2YEAR", date(2026, 8, 3), "4.65", "2-year"))

    page = client.get("/markets").text
    assert "nothing more" in page, "the restraint should be stated, not merely practised"

    data = re.sub(r'<p class="muted small">.*?</p>', "", page, flags=re.S).lower()
    for forbidden in ("inverted", "inversion", "recession", "signal", "regime", "bullish"):
        assert forbidden not in data


def test_an_unconfirmed_earnings_session_is_not_rendered_as_a_time(
    client: TestClient, migrated: Any
) -> None:
    """A catalyst calendar implying a known moment can be wrong by sixteen
    hours."""
    with migrated.sessions.begin() as session:
        session.add(
            EarningsEventRow(
                symbol="XMPL",
                reports_on=date.today(),
                company="Example Lithium Corp",
                time_of_day="time-not-supplied",
                is_confirmed=False,
                source="nasdaq",
                retrieved_at=datetime.now(UTC),
            )
        )

    body = client.get("/markets").text
    assert "unconfirmed" in body
    assert "time-not-supplied" not in body


def test_a_missing_consensus_is_an_em_dash_not_zero(
    client: TestClient, migrated: Any
) -> None:
    """Zero would read as "expected to break even", a different claim."""
    with migrated.sessions.begin() as session:
        session.add(
            EarningsEventRow(
                symbol="XMPL",
                reports_on=date.today(),
                company="Example Lithium Corp",
                eps_forecast=None,
                is_confirmed=True,
                time_of_day="time-pre-market",
                source="nasdaq",
                retrieved_at=datetime.now(UTC),
            )
        )

    body = client.get("/markets").text
    assert "—" in body
    assert "$0.00" not in body


def test_the_jobs_page_leads_with_the_last_success_not_the_last_run(
    client: TestClient, migrated: Any
) -> None:
    """"It ran an hour ago" and "it last worked four days ago" can both be
    true, and only the second one matters."""
    with migrated.sessions.begin() as session:
        session.add(
            JobRunRow(
                run_id="ok-1",
                job="refresh",
                status="done",
                source="local",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                message="9 stage(s) clean",
            )
        )

    body = client.get("/jobs").text
    assert "Last success" in body
    assert "9 stage(s) clean" in body


def test_an_abandoned_run_reads_as_abandoned_not_as_running(
    client: TestClient, migrated: Any
) -> None:
    """A crashed run whose lock was taken over must not read as "running"
    forever. The service already marks it; the page has to surface that
    rather than show a spinner nobody can clear."""
    with migrated.sessions.begin() as session:
        session.add(
            JobRunRow(
                run_id="dead-1",
                job="refresh",
                status="abandoned",
                source="local",
                started_at=datetime.now(UTC),
                error="lock taken over",
            )
        )

    body = client.get("/jobs").text
    assert "abandoned" in body
    assert "lock taken over" in body


def test_the_ai_ceiling_is_labelled_as_counted_in_utc(client: TestClient) -> None:
    """The window and the ledger have to agree on which day it is, and the
    page says which day that is so a reader can check."""
    assert "UTC" in client.get("/jobs").text


def test_the_health_api_survives_a_finding_that_has_a_timestamp(
    client: TestClient, migrated: Any
) -> None:
    """The monitor endpoint 500'd on every machine that had data.

    An open finding carries a real `detected_at` datetime and
    `JSONResponse` uses plain `json.dumps`, which raises on one — so
    `/api/health` returned 500 as soon as anything had been recorded, and
    200 only against the empty database the tests happened to use. A
    monitor that breaks precisely when there is something to report fails
    identically whether the system is healthy or gone.
    """
    from akmarkets.services import market_data

    with migrated.sessions.begin() as session:
        market_data.record_finding(
            session,
            code="price.disagreement",
            severity="warning",
            subject_type="instrument",
            subject_id="AAPL",
            message="two providers disagree by 3%",
        )

    response = client.get("/api/health")
    assert response.status_code in {200, 503}, response.text
    finding = response.json()["findings"][0]
    # Serialised, not dropped: the timestamp is the point of the field.
    assert isinstance(finding["detected_at"], str)
    assert finding["detected_at"].startswith("20")
