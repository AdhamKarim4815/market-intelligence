"""Macro rates and the earnings calendar — both keyless, both official.

**FRED was measured and rejected, 2026-08-10.** It was the obvious choice
and it is what the plan called for, but its keyless CSV endpoint
(`fredgraph.csv`) returns an HTTP/2 stream error and then times out from
here; the API proper needs an account. Two sources cover the same ground
with no key and no account at all:

- **US Treasury `fiscaldata` API** and the daily par yield curve — the
  primary source FRED itself redistributes for rates. No key, documented,
  and one hop closer to the issuer.
- **Nasdaq's earnings calendar** — the same endpoint family already used
  for prices, which Finnhub's free tier was going to supply.

Needing no credential is not a consolation here. A key is a thing that
expires, leaks, hits a quota and has to be rotated; two fewer of them is a
smaller system, and one that a fresh clone can run immediately.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import httpx
from defusedxml import ElementTree

from akmarkets.providers.base import Provenance, ProviderResult

DEFAULT_TIMEOUT = 30.0

_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if text in {"", "N/A", "n/a", "null", "None", "-", "--"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class MacroSeries:
    """One macro series, newest last.

    `unit` travels with the values because a rate and a level are both
    numbers and mistaking one for the other is silent. Nothing here
    annualises, rebases or seasonally adjusts — the figures are as
    published.
    """

    series_id: str
    name: str
    unit: str
    points: tuple[tuple[date, Decimal], ...]

    @property
    def latest(self) -> tuple[date, Decimal] | None:
        return self.points[-1] if self.points else None

    def change_over(self, days: int) -> Decimal | None:
        """Change against the observation closest to `days` ago.

        None rather than an approximation when the history is too short:
        "the 10-year moved 40bp this year" from three weeks of data is not
        a smaller version of the truth.
        """
        if len(self.points) < 2:
            return None
        latest_on, latest_value = self.points[-1]
        target = latest_on.toordinal() - days
        earlier = [point for point in self.points if point[0].toordinal() <= target]
        if not earlier:
            return None
        return latest_value - earlier[-1][1]


class TreasuryProvider:
    """US Treasury rates. Keyless, official, and the source FRED mirrors."""

    name = "us-treasury"
    YIELD_CURVE = (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/pages/xml"
    )
    FISCAL_DATA = (
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/"
        "accounting/od/avg_interest_rates"
    )

    # The tenors worth carrying. A yield curve has a dozen points and only a
    # few of them ever appear in a sentence anybody reads.
    TENORS: ClassVar[dict[str, str]] = {
        "BC_3MONTH": "3-month",
        "BC_2YEAR": "2-year",
        "BC_10YEAR": "10-year",
        "BC_30YEAR": "30-year",
    }

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, client: httpx.Client | None = None):
        self._timeout = timeout
        self._client = client

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        client = self._client or httpx.Client(timeout=self._timeout)
        response = client.get(url, params=params, headers={"User-Agent": _AGENT})
        response.raise_for_status()
        return response

    def fetch_yield_curve(
        self, year: int | None = None, years: int = 2
    ) -> ProviderResult[list[MacroSeries]]:
        """Daily par yields for the tenors that matter.

        The feed is one XML document per calendar year, so `years` fetches
        that many back. Two by default, because the most common question —
        "where are rates versus a year ago" — spans a year boundary for
        most of the year, and a single document would answer it `None` for
        eleven months out of twelve.
        """
        target = year or date.today().year
        collected: dict[str, list[tuple[date, Decimal]]] = {key: [] for key in self.TENORS}
        failures: list[str] = []

        for offset in range(years):
            outcome = self._collect_year(target - offset, collected)
            if outcome:
                failures.append(outcome)

        if not any(collected.values()):
            return ProviderResult.failed(
                self.name, "; ".join(failures) or "no yield observations"
            )

        series = [
            MacroSeries(
                series_id=tenor,
                name=f"US Treasury {label} par yield",
                unit="percent",
                points=tuple(sorted(points)),
            )
            for tenor, label in self.TENORS.items()
            if (points := collected[tenor])
        ]
        latest = max(point[0] for item in series for point in item.points)
        return ProviderResult.healthy(
            series,
            Provenance(
                source=self.name,
                retrieved_at=datetime.now(UTC),
                effective_at=datetime.combine(latest, datetime.min.time(), tzinfo=UTC),
                url=self.YIELD_CURVE,
            ),
        )

    def _collect_year(
        self, target: int, collected: dict[str, list[tuple[date, Decimal]]]
    ) -> str | None:
        """Read one year's document into `collected`. Returns an error, or None."""
        try:
            response = self._get(
                self.YIELD_CURVE,
                params={
                    "data": "daily_treasury_yield_curve",
                    "field_tdr_date_value": str(target),
                },
            )
            root = ElementTree.fromstring(response.text)
        except Exception as exc:
            return f"{target}: {type(exc).__name__}"

        # The document is Atom; the figures live in a namespaced properties
        # element. Tags are matched by local name so a namespace revision
        # does not silently return an empty series.
        for entry in root.iter():
            if not entry.tag.endswith("properties"):
                continue
            observed: date | None = None
            values: dict[str, Decimal] = {}
            for field in entry:
                local = field.tag.rsplit("}", 1)[-1]
                if local == "NEW_DATE" and field.text:
                    try:
                        observed = datetime.fromisoformat(
                            field.text.replace("Z", "+00:00")
                        ).date()
                    except ValueError:
                        observed = None
                elif local in self.TENORS:
                    parsed = _decimal(field.text)
                    if parsed is not None:
                        values[local] = parsed
            if observed is None:
                continue
            for tenor, value in values.items():
                collected[tenor].append((observed, value))
        return None

    def fetch_average_rates(self, limit: int = 12) -> ProviderResult[list[MacroSeries]]:
        """Average interest rates on outstanding Treasury debt."""
        try:
            payload = self._get(
                self.FISCAL_DATA,
                params={"page[size]": str(limit), "sort": "-record_date"},
            ).json()
        except Exception as exc:
            return ProviderResult.failed(self.name, f"{type(exc).__name__}: {exc}")

        rows = payload.get("data") or []
        grouped: dict[str, list[tuple[date, Decimal]]] = {}
        for row in rows:
            value = _decimal(row.get("avg_interest_rate_amt"))
            raw_date = row.get("record_date")
            description = str(row.get("security_desc") or "").strip()
            if value is None or not raw_date or not description:
                continue
            try:
                observed = date.fromisoformat(str(raw_date))
            except ValueError:
                continue
            grouped.setdefault(description, []).append((observed, value))

        series = [
            MacroSeries(
                series_id=description.lower().replace(" ", "-"),
                name=f"Average rate, {description}",
                unit="percent",
                points=tuple(sorted(points)),
            )
            for description, points in grouped.items()
        ]
        if not series:
            return ProviderResult.missing(self.name, "no average-rate rows returned")
        return ProviderResult.healthy(
            series, Provenance(source=self.name, retrieved_at=datetime.now(UTC))
        )


@dataclass(frozen=True)
class EarningsEvent:
    """A scheduled or reported earnings date.

    `eps_forecast` is the published consensus where one exists. It is
    carried as given and never used to compute a surprise here — that
    subtraction belongs in the domain, against an actual that has been
    reported.
    """

    symbol: str
    company: str
    on: date
    eps_forecast: Decimal | None = None
    estimate_count: int | None = None
    last_year_eps: Decimal | None = None
    time_of_day: str = ""

    @property
    def is_confirmed(self) -> bool:
        """Whether the time of day is known.

        A date with no session marker is a scheduled *day*, not a scheduled
        moment, and a catalyst calendar that implies otherwise will be
        wrong by up to sixteen hours.
        """
        return self.time_of_day not in {"", "time-not-supplied"}


class NasdaqEarningsProvider:
    """The earnings calendar, from the same keyless API used for prices.

    This is what Finnhub's free tier was going to supply. Using the source
    already in the fallback chain means one fewer provider to monitor and
    one fewer credential to rotate.
    """

    name = "nasdaq"
    CALENDAR = "https://api.nasdaq.com/api/calendar/earnings"
    SURPRISE = "https://api.nasdaq.com/api/company/{symbol}/earnings-surprise"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, client: httpx.Client | None = None):
        self._timeout = timeout
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": _AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def fetch_calendar(self, on: date) -> ProviderResult[list[EarningsEvent]]:
        """Every company reporting on one date."""
        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            response = client.get(
                self.CALENDAR, params={"date": on.isoformat()}, headers=self._headers()
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return ProviderResult.failed(self.name, f"{type(exc).__name__}: {exc}")

        rows = ((payload.get("data") or {}).get("rows")) or []
        events = [
            EarningsEvent(
                symbol=str(row.get("symbol") or "").upper(),
                company=str(row.get("name") or ""),
                on=on,
                eps_forecast=_decimal(row.get("epsForecast")),
                estimate_count=int(count) if (count := _decimal(row.get("noOfEsts"))) else None,
                last_year_eps=_decimal(row.get("lastYearEPS")),
                time_of_day=str(row.get("time") or ""),
            )
            for row in rows
            if row.get("symbol")
        ]
        if not events:
            # A day with no reports is a fact about the calendar, not a
            # failure — most days are quiet and every weekend is empty.
            return ProviderResult.missing(self.name, f"nothing scheduled for {on}")
        return ProviderResult.healthy(
            events,
            Provenance(
                source=self.name,
                retrieved_at=datetime.now(UTC),
                effective_at=datetime.combine(on, datetime.min.time(), tzinfo=UTC),
            ),
        )

    def fetch_for_symbols(
        self, symbols: Sequence[str], dates: Sequence[date]
    ) -> ProviderResult[list[EarningsEvent]]:
        """Only the reports that concern a given set of symbols.

        Filtered client-side because the calendar endpoint is per-date, not
        per-symbol. Fetching a handful of dates and filtering costs fewer
        requests than asking per symbol, and the response is small.
        """
        wanted = {symbol.upper() for symbol in symbols}
        found: list[EarningsEvent] = []
        failures: list[str] = []

        for on in dates:
            result = self.fetch_calendar(on)
            if result.value:
                found.extend(event for event in result.value if event.symbol in wanted)
            elif result.state.value == "failed":
                failures.append(f"{on}: {result.message[:40]}")

        if failures and not found:
            return ProviderResult.failed(self.name, "; ".join(failures[:3]))
        if not found:
            return ProviderResult.missing(
                self.name, "none of these symbols report in the window"
            )
        return ProviderResult.healthy(
            found, Provenance(source=self.name, retrieved_at=datetime.now(UTC))
        )
