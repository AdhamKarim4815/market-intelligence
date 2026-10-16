"""Storing and reading back rates and the earnings calendar.

The provider layer is covered in `test_macro.py` against stubbed HTTP.
What is tested here is what happens to the data afterwards: revisions,
missing consensus, and the refusal to answer a one-year question with
three weeks of history.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.db.models import EarningsEventRow, MacroObservationRow
from akmarkets.providers.base import Provenance, ProviderResult
from akmarkets.providers.macro import EarningsEvent, MacroSeries
from akmarkets.services import macro as macro_service


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


def series(series_id: str, points: list[tuple[date, str]]) -> MacroSeries:
    return MacroSeries(
        series_id=series_id,
        name=f"US Treasury {series_id}",
        unit="percent",
        points=tuple((on, Decimal(value)) for on, value in points),
    )


class StubTreasury:
    """A Treasury provider that returns whatever it was handed."""

    name = "us-treasury"

    def __init__(self, result: ProviderResult[list[MacroSeries]]) -> None:
        self._result = result

    def fetch_yield_curve(self, year: int | None = None, years: int = 2):  # type: ignore[no-untyped-def]
        return self._result


class StubNasdaq:
    name = "nasdaq"

    def __init__(self, result: ProviderResult[list[EarningsEvent]]) -> None:
        self._result = result
        self.asked_for: list[date] = []

    def fetch_for_symbols(self, symbols, dates):  # type: ignore[no-untyped-def]
        self.asked_for = list(dates)
        return self._result


def healthy(value):  # type: ignore[no-untyped-def]
    return ProviderResult.healthy(
        value, Provenance(source="stub", retrieved_at=datetime.now(UTC))
    )


# -- rates ---------------------------------------------------------------


def test_a_revised_observation_corrects_the_day_rather_than_duplicating(
    session: Session,
) -> None:
    """Treasury revises published figures. Two rows for one day would leave
    a reader guessing which is current."""
    settings = Settings()
    day = date(2026, 8, 3)

    macro_service.refresh_macro(
        session, settings, provider=StubTreasury(healthy([series("BC_10YEAR", [(day, "4.10")])]))
    )
    macro_service.refresh_macro(
        session, settings, provider=StubTreasury(healthy([series("BC_10YEAR", [(day, "4.17")])]))
    )
    session.flush()

    rows = session.query(MacroObservationRow).all()
    assert len(rows) == 1
    assert rows[0].value == Decimal("4.17")


def test_a_failed_feed_stores_nothing_and_says_so(session: Session) -> None:
    """Degraded, not raised — and explicitly not silently successful."""
    outcome = macro_service.refresh_macro(
        session,
        Settings(),
        provider=StubTreasury(ProviderResult.failed("us-treasury", "timeout")),
    )
    assert outcome["items"] == 0
    assert outcome["degraded"] is True
    assert "timeout" in str(outcome["detail"])
    assert session.query(MacroObservationRow).count() == 0


def test_a_change_is_none_when_the_history_is_too_short(session: Session) -> None:
    """"The 10-year is unchanged on the year" from three weeks of data is
    not a smaller version of the truth."""
    macro_service.refresh_macro(
        session,
        Settings(),
        provider=StubTreasury(
            healthy(
                [
                    series(
                        "BC_10YEAR",
                        [(date(2026, 7, 20), "4.00"), (date(2026, 8, 3), "4.20")],
                    )
                ]
            )
        ),
    )
    session.flush()

    snapshot = macro_service.latest_rates(session)[0]
    assert snapshot.value == Decimal("4.20")
    assert snapshot.change_1m is None
    assert snapshot.change_1y is None


def test_a_change_is_computed_once_the_history_supports_it(session: Session) -> None:
    macro_service.refresh_macro(
        session,
        Settings(),
        provider=StubTreasury(
            healthy(
                [
                    series(
                        "BC_10YEAR",
                        [(date(2026, 6, 1), "3.80"), (date(2026, 8, 3), "4.20")],
                    )
                ]
            )
        ),
    )
    session.flush()

    snapshot = macro_service.latest_rates(session)[0]
    assert snapshot.change_1m == Decimal("0.40")
    assert snapshot.change_1y is None


def test_describe_rate_always_prints_the_unit(session: Session) -> None:
    """A rate and a change in a rate are both small numbers, and mistaking
    one for the other is silent."""
    snapshot = macro_service.RateSnapshot(
        series_id="BC_10YEAR",
        name="US Treasury 10-year par yield",
        observed_on=date(2026, 8, 3),
        value=Decimal("4.20"),
        unit="percent",
        change_1m=Decimal("0.40"),
    )
    line = macro_service.describe_rate(snapshot)
    assert "4.20%" in line
    assert "+0.40" in line
    assert "2026-08-03" in line


# -- catalysts -----------------------------------------------------------


def event(symbol: str, on: date, forecast: str | None = "1.50", when: str = "") -> EarningsEvent:
    return EarningsEvent(
        symbol=symbol,
        company=f"{symbol} Inc",
        on=on,
        eps_forecast=Decimal(forecast) if forecast is not None else None,
        time_of_day=when,
    )


def test_the_calendar_is_only_asked_about_weekdays(session: Session) -> None:
    """Companies do not report at the weekend, and asking costs a request
    that can only return nothing."""
    stub = StubNasdaq(healthy([]))
    macro_service.refresh_catalysts(
        session, Settings(), ["AAPL"], today=date(2026, 8, 3), provider=stub
    )
    assert stub.asked_for
    assert all(day.weekday() < 5 for day in stub.asked_for)


def test_no_consensus_is_stored_as_none_not_zero(session: Session) -> None:
    """Zero would be read as "expected to break even", a different claim."""
    macro_service.refresh_catalysts(
        session,
        Settings(),
        ["XMPL"],
        today=date(2026, 8, 3),
        provider=StubNasdaq(healthy([event("XMPL", date(2026, 8, 14), forecast=None)])),
    )
    session.flush()

    row = session.query(EarningsEventRow).one()
    assert row.eps_forecast is None
    assert "no consensus" in macro_service.describe_earnings(row)


def test_an_unconfirmed_session_is_not_rendered_as_a_time(session: Session) -> None:
    """A calendar implying a known moment can be wrong by sixteen hours."""
    macro_service.refresh_catalysts(
        session,
        Settings(),
        ["AAPL"],
        today=date(2026, 8, 3),
        provider=StubNasdaq(
            healthy([event("AAPL", date(2026, 8, 6), when="time-not-supplied")])
        ),
    )
    session.flush()

    row = session.query(EarningsEventRow).one()
    assert row.is_confirmed is False
    assert "unconfirmed" in macro_service.describe_earnings(row)


def test_a_revised_estimate_updates_the_report_rather_than_adding_one(
    session: Session,
) -> None:
    settings = Settings()
    for forecast in ("1.50", "1.62"):
        macro_service.refresh_catalysts(
            session,
            settings,
            ["AAPL"],
            today=date(2026, 8, 3),
            provider=StubNasdaq(healthy([event("AAPL", date(2026, 8, 6), forecast=forecast)])),
        )
    session.flush()

    rows = session.query(EarningsEventRow).all()
    assert len(rows) == 1
    assert rows[0].eps_forecast == Decimal("1.62")


def test_upcoming_earnings_excludes_what_has_already_happened(session: Session) -> None:
    macro_service.refresh_catalysts(
        session,
        Settings(),
        ["AAPL", "XMPL"],
        today=date(2026, 8, 3),
        provider=StubNasdaq(
            healthy([event("AAPL", date(2026, 8, 6)), event("XMPL", date(2026, 8, 14))])
        ),
    )
    session.flush()

    later = macro_service.upcoming_earnings(session, today=date(2026, 8, 10))
    assert [row.symbol for row in later] == ["XMPL"]


def test_no_symbols_makes_no_requests(session: Session) -> None:
    """An empty universe must not fetch a calendar it cannot use."""
    stub = StubNasdaq(healthy([]))
    outcome = macro_service.refresh_catalysts(session, Settings(), [], provider=stub)
    assert outcome["items"] == 0
    assert stub.asked_for == []


def test_display_drops_storage_padding_without_going_exponential() -> None:
    """The column keeps eight decimal places, so a published 4.65 reads back
    as 4.65000000 and an unchanged rate as 0E-8. Bare normalize() fixes both
    and turns 100 into 1E+2, which is why `trim` exists."""
    assert str(macro_service.trim(Decimal("4.65000000"))) == "4.65"
    assert str(macro_service.trim(Decimal("0E-8"))) == "0"
    assert str(macro_service.trim(Decimal("100.00000000"))) == "100"
    assert str(macro_service.trim(Decimal("0.09000000"))) == "0.09"


def test_an_unchanged_rate_reads_as_zero_not_scientific_notation() -> None:
    snapshot = macro_service.RateSnapshot(
        series_id="BC_3MONTH",
        name="US Treasury 3-month par yield",
        observed_on=date(2026, 8, 7),
        value=Decimal("3.87000000"),
        unit="percent",
        change_1m=Decimal("0E-8"),
    )
    line = macro_service.describe_rate(snapshot)
    assert "3.87%" in line
    assert "1m +0.00" in line
    assert "E-" not in line
