"""SEC EDGAR: filed fundamentals and the filings themselves.

The best free financial data that exists, and the only one in this project
whose numbers come from a document somebody signed. No key, no rate plan —
only two conditions of use that are honoured here because they are
conditions, not tuning: a contactable User-Agent, and ten requests a second.

Two structural facts about EDGAR shape this module.

**It does not know tickers.** Everything is keyed by CIK, so a separate
mapping file has to be fetched and cached. A ticker that is not in it is
absent, not an error.

**Plenty of listed securities file nothing here.** Foreign issuers report
under IFRS on 20-F/40-F and use no `us-gaap` concepts at all; ETFs and unit
trusts have a CIK but no XBRL facts and answer 404. Both are facts about the
security rather than failures, and are reported as absent — otherwise every
nightly run marks this provider degraded because a benchmark is in the
universe. Measured 2026-08-10: AAPL returns 2,566 facts, XMPL (Brazilian,
IFRS) returns none, SPY 404s.

**A company reports the concepts it chooses.** `Revenues`,
`RevenueFromContractWithCustomerExcludingAssessedTax` and
`SalesRevenueNet` are all revenue, used by different filers in different
years, and nothing in the response says which one this company means. The
concept lists below are ordered by preference and tried in turn; a company
that uses none of them has no revenue figure, and that is reported rather
than guessed at.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from akmarkets.domain.market import ReportedFact
from akmarkets.providers.base import Provenance, ProviderResult

# EDGAR asks for ten requests a second. Honoured with a real gate rather
# than a sleep between calls, because the price cache fetches concurrently
# and a per-call sleep would not bound the aggregate rate.
_DEFAULT_RATE = 8.0


class RateGate:
    """A shared minimum interval between requests, safe across threads."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if wait_for:
            time.sleep(wait_for)


# Ordered by preference. Earlier entries are the concept a modern filer is
# most likely to use; later ones are what older or smaller filers used.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "eps_diluted": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "total_debt": (
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "DebtLongtermAndShorttermCombinedAmount",
    ),
    "total_assets": ("Assets",),
    "total_equity": ("StockholdersEquity",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "shares_diluted": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
}


# `Fact` is the domain's `ReportedFact`. Aliased rather than redefined so
# an adapter reads naturally without the domain depending on this module.
Fact = ReportedFact


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


class EdgarProvider:
    """Company facts and filings from SEC EDGAR."""

    name = "sec-edgar"
    TICKERS = "https://www.sec.gov/files/company_tickers.json"
    FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"

    def __init__(
        self,
        user_agent: str,
        requests_per_second: float = _DEFAULT_RATE,
        timeout: float = 45.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._timeout = timeout
        self._client = client
        self._gate = RateGate(requests_per_second)
        self._tickers: dict[str, str] | None = None

    def _get(self, url: str) -> httpx.Response:
        self._gate.wait()
        client = self._client or httpx.Client(timeout=self._timeout)
        response = client.get(url, headers=self._headers)
        response.raise_for_status()
        return response

    # -- identity -------------------------------------------------------

    def cik_for(self, symbol: str) -> ProviderResult[str]:
        """Map a ticker to a zero-padded CIK.

        Cached for the life of the provider: the file is a megabyte and
        changes rarely, and fetching it per lookup would spend the entire
        rate budget on a mapping that did not move.
        """
        if self._tickers is None:
            try:
                payload = self._get(self.TICKERS).json()
            except Exception as exc:
                return ProviderResult.failed(self.name, f"{type(exc).__name__}: {exc}")
            self._tickers = {
                str(entry["ticker"]).upper(): f"{int(entry['cik_str']):010d}"
                for entry in payload.values()
                if entry.get("ticker")
            }

        cik = self._tickers.get(symbol.strip().upper())
        if cik is None:
            # Foreign issuers and most ETFs are genuinely not here. Absent,
            # not broken, and the next provider may still know the symbol.
            return ProviderResult.missing(
                self.name, f"{symbol} is not in the SEC ticker file"
            )
        return ProviderResult.healthy(
            cik, Provenance(source=self.name, retrieved_at=datetime.now(UTC))
        )

    # -- fundamentals ---------------------------------------------------

    def fetch_facts(
        self, symbol: str, metrics: Sequence[str] | None = None
    ) -> ProviderResult[list[Fact]]:
        """Every reported value for the metrics we know how to find.

        Returns the whole history rather than the latest, because "revenue
        grew" needs two points and re-fetching to get the second would
        double the request count for data that arrived in the first
        response.
        """
        resolved = self.cik_for(symbol)
        if not resolved.ok or resolved.value is None:
            return ProviderResult(
                value=None, state=resolved.state, message=resolved.message
            )

        try:
            payload = self._get(self.FACTS.format(cik=resolved.value)).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # The CIK exists but files no XBRL — every ETF and unit
                # trust is like this. A fact about the security, not a
                # transport failure, and conflating them would mark the
                # provider degraded every night because a benchmark is in
                # the universe.
                return ProviderResult.missing(
                    self.name, f"{symbol} files no XBRL company facts"
                )
            return ProviderResult.failed(self.name, f"{symbol}: HTTP {exc.response.status_code}")
        except Exception as exc:
            return ProviderResult.failed(self.name, f"{symbol}: {type(exc).__name__}: {exc}")

        us_gaap = (payload.get("facts") or {}).get("us-gaap") or {}
        wanted = list(metrics) if metrics else list(CONCEPTS)
        facts: list[Fact] = []

        for metric in wanted:
            for concept in CONCEPTS.get(metric, ()):
                entry = us_gaap.get(concept)
                if not entry:
                    continue
                # Units vary (USD, USD/shares, shares). Whichever this
                # concept reports in is taken as-is; converting between them
                # would be inventing a number.
                for records in (entry.get("units") or {}).values():
                    for record in records:
                        value = _decimal(record.get("val"))
                        end = record.get("end")
                        if value is None or not end:
                            continue
                        try:
                            period_end = date.fromisoformat(end)
                        except ValueError:
                            continue
                        facts.append(
                            Fact(
                                metric=metric,
                                concept=concept,
                                value=value,
                                period_end=period_end,
                                fiscal_year=record.get("fy"),
                                fiscal_period=record.get("fp"),
                                form=str(record.get("form") or ""),
                                filed_on=(
                                    date.fromisoformat(record["filed"])
                                    if record.get("filed")
                                    else None
                                ),
                            )
                        )
                # The first concept that produced anything wins. Merging
                # two concepts for one metric would splice two different
                # definitions into one series.
                if any(fact.metric == metric for fact in facts):
                    break

        if not facts:
            return ProviderResult.missing(
                self.name, f"{symbol} reports none of the expected concepts"
            )
        return ProviderResult.healthy(
            facts,
            Provenance(
                source=self.name,
                retrieved_at=datetime.now(UTC),
                effective_at=datetime.combine(
                    max(fact.period_end for fact in facts),
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                url=self.FACTS.format(cik=resolved.value),
            ),
        )

    # -- filings --------------------------------------------------------

    def fetch_filings(
        self, symbol: str, forms: Sequence[str] = ("8-K", "10-Q", "10-K"), limit: int = 20
    ) -> ProviderResult[list[dict[str, str]]]:
        """Recent filings of the given forms.

        A filing is the highest-credibility news this system can see, which
        is why it is collected separately from the news feed rather than
        hoped for within it.
        """
        resolved = self.cik_for(symbol)
        if not resolved.ok or resolved.value is None:
            return ProviderResult(
                value=None, state=resolved.state, message=resolved.message
            )
        try:
            payload = self._get(self.SUBMISSIONS.format(cik=resolved.value)).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ProviderResult.missing(self.name, f"{symbol} has no submissions")
            return ProviderResult.failed(self.name, f"{symbol}: HTTP {exc.response.status_code}")
        except Exception as exc:
            return ProviderResult.failed(self.name, f"{symbol}: {type(exc).__name__}: {exc}")

        recent = ((payload.get("filings") or {}).get("recent")) or {}
        wanted = {form.upper() for form in forms}
        rows: list[dict[str, str]] = []
        for index, form in enumerate(recent.get("form", [])):
            if form.upper() not in wanted:
                continue
            accession = str(recent.get("accessionNumber", [])[index]).replace("-", "")
            document = str(recent.get("primaryDocument", [])[index])
            rows.append(
                {
                    "form": str(form),
                    "filed_on": str(recent.get("filingDate", [])[index]),
                    "title": str(recent.get("primaryDocDescription", [])[index] or form),
                    "url": (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{int(resolved.value)}/{accession}/{document}"
                    ),
                }
            )
            if len(rows) >= limit:
                break

        if not rows:
            return ProviderResult.missing(self.name, f"{symbol} has no matching filings")
        return ProviderResult.healthy(
            rows, Provenance(source=self.name, retrieved_at=datetime.now(UTC))
        )
