"""Keyless macro rates and the earnings calendar."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from akmarkets.providers.base import DataState
from akmarkets.providers.macro import (
    MacroSeries,
    NasdaqEarningsProvider,
    TreasuryProvider,
)

# One properties entry, in the Atom shape the Treasury actually serves.
YIELD_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
  <entry><content><m:properties>
    <d:NEW_DATE>2026-08-06T00:00:00</d:NEW_DATE>
    <d:BC_3MONTH>3.90</d:BC_3MONTH><d:BC_2YEAR>4.10</d:BC_2YEAR>
    <d:BC_10YEAR>4.50</d:BC_10YEAR><d:BC_30YEAR>5.10</d:BC_30YEAR>
  </m:properties></content></entry>
  <entry><content><m:properties>
    <d:NEW_DATE>2026-08-07T00:00:00</d:NEW_DATE>
    <d:BC_3MONTH>3.87</d:BC_3MONTH><d:BC_2YEAR>4.19</d:BC_2YEAR>
    <d:BC_10YEAR>4.65</d:BC_10YEAR><d:BC_30YEAR>5.19</d:BC_30YEAR>
  </m:properties></content></entry>
</feed>"""

CALENDAR = {
    "data": {
        "rows": [
            {"symbol": "XMPL", "name": "Example Lithium Corp", "epsForecast": "$0.15",
             "noOfEsts": "1", "lastYearEPS": "$0.09", "time": "time-pre-market"},
            {"symbol": "OTHER", "name": "Other Co", "epsForecast": "N/A",
             "noOfEsts": "", "lastYearEPS": "", "time": "time-not-supplied"},
        ]
    }
}


def stub(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


# -- treasury -----------------------------------------------------------


def test_the_yield_curve_parses_into_one_series_per_tenor() -> None:
    provider = TreasuryProvider(
        client=stub(lambda request: httpx.Response(200, text=YIELD_XML))
    )
    result = provider.fetch_yield_curve(year=2026, years=1)
    assert result.ok
    assert result.value is not None
    names = {series.series_id for series in result.value}
    assert names == {"BC_3MONTH", "BC_2YEAR", "BC_10YEAR", "BC_30YEAR"}

    ten_year = next(s for s in result.value if s.series_id == "BC_10YEAR")
    assert ten_year.latest == (date(2026, 8, 7), Decimal("4.65"))
    assert ten_year.unit == "percent"


def test_tags_are_matched_by_local_name_so_a_namespace_change_is_survivable() -> None:
    """Matching the fully-qualified tag would silently return an empty
    series the day the Treasury revises its schema."""
    shifted = YIELD_XML.replace(
        "http://schemas.microsoft.com/ado/2007/08/dataservices",
        "http://example.gov/newschema",
    )
    provider = TreasuryProvider(
        client=stub(lambda request: httpx.Response(200, text=shifted))
    )
    assert provider.fetch_yield_curve(year=2026, years=1).ok


def test_two_years_are_fetched_so_a_one_year_change_is_answerable() -> None:
    """A single document answers "versus a year ago" with None for eleven
    months out of twelve."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("field_tdr_date_value"))
        return httpx.Response(200, text=YIELD_XML)

    TreasuryProvider(client=stub(handler)).fetch_yield_curve(year=2026)
    assert seen == ["2026", "2025"]


def test_one_bad_year_does_not_lose_the_other() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("field_tdr_date_value") == "2025":
            return httpx.Response(500, text="nope")
        return httpx.Response(200, text=YIELD_XML)

    result = TreasuryProvider(client=stub(handler)).fetch_yield_curve(year=2026)
    assert result.ok


def test_a_change_over_a_window_longer_than_the_history_is_none() -> None:
    """"The 10-year moved 40bp this year" from three weeks of data is not a
    smaller version of the truth."""
    series = MacroSeries(
        series_id="x", name="x", unit="percent",
        points=((date(2026, 8, 6), Decimal("4.50")), (date(2026, 8, 7), Decimal("4.65"))),
    )
    assert series.change_over(1) == Decimal("0.15")
    assert series.change_over(365) is None


def test_a_total_failure_is_reported_as_failed() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("down")

    assert TreasuryProvider(client=stub(boom)).fetch_yield_curve().state is DataState.FAILED


# -- earnings calendar --------------------------------------------------


def test_the_calendar_parses_display_formatted_forecasts() -> None:
    provider = NasdaqEarningsProvider(
        client=stub(lambda request: httpx.Response(200, json=CALENDAR))
    )
    result = provider.fetch_calendar(date(2026, 8, 14))
    assert result.ok
    assert result.value is not None
    xmpl = next(event for event in result.value if event.symbol == "XMPL")
    assert xmpl.eps_forecast == Decimal("0.15")
    assert xmpl.estimate_count == 1


def test_a_date_with_no_session_marker_is_not_a_scheduled_moment() -> None:
    """A catalyst calendar that implies otherwise will be wrong by up to
    sixteen hours."""
    provider = NasdaqEarningsProvider(
        client=stub(lambda request: httpx.Response(200, json=CALENDAR))
    )
    events = provider.fetch_calendar(date(2026, 8, 14)).value or []
    by_symbol = {event.symbol: event for event in events}
    assert by_symbol["XMPL"].is_confirmed is True
    assert by_symbol["OTHER"].is_confirmed is False


def test_a_quiet_day_is_absent_not_failed() -> None:
    """Most days are quiet and every weekend is empty."""
    provider = NasdaqEarningsProvider(
        client=stub(lambda request: httpx.Response(200, json={"data": {"rows": []}}))
    )
    assert provider.fetch_calendar(date(2026, 8, 15)).state is DataState.UNKNOWN


def test_filtering_by_symbol_keeps_only_what_was_asked_for() -> None:
    provider = NasdaqEarningsProvider(
        client=stub(lambda request: httpx.Response(200, json=CALENDAR))
    )
    result = provider.fetch_for_symbols(["xmpl"], [date(2026, 8, 14)])
    assert result.value is not None
    assert [event.symbol for event in result.value] == ["XMPL"]


def test_no_matching_symbol_in_the_window_is_absent_not_failed() -> None:
    provider = NasdaqEarningsProvider(
        client=stub(lambda request: httpx.Response(200, json=CALENDAR))
    )
    result = provider.fetch_for_symbols(["NVDA"], [date(2026, 8, 14)])
    assert result.state is DataState.UNKNOWN


@pytest.mark.parametrize("raw", ["N/A", "", "--"])
def test_an_absent_forecast_is_none_not_zero(raw: str) -> None:
    payload = {"data": {"rows": [{"symbol": "X", "name": "X", "epsForecast": raw}]}}
    provider = NasdaqEarningsProvider(
        client=stub(lambda request: httpx.Response(200, json=payload))
    )
    events = provider.fetch_calendar(date(2026, 8, 14)).value or []
    assert events[0].eps_forecast is None
