"""Provider health and data-quality findings, against the real schema."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from akmarkets.db.engine import Database
from akmarkets.db.models import ProviderHealthRow
from akmarkets.providers.base import Bar, DataState, Disagreement, ProviderResult
from akmarkets.providers.cache import PriceCache
from akmarkets.providers.chain import HistoryChain
from akmarkets.services import market_data


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


class FakeHistory:
    def __init__(self, name: str, result: ProviderResult[list[Bar]]) -> None:
        self.name = name
        self._result = result

    def fetch_history(self, symbol: str, start: date, end: date) -> ProviderResult[list[Bar]]:
        return self._result


def _bar(on: date) -> Bar:
    return Bar(
        on=on, open=Decimal("10"), high=Decimal("11"),
        low=Decimal("9"), close=Decimal("10.5"), volume=1000,
    )


# -- provider health ----------------------------------------------------


def test_a_success_clears_the_consecutive_failure_count(session: Session) -> None:
    """One timeout is normal on a free endpoint and should not turn a
    dashboard red; five in a row is a provider that has stopped working."""
    for _ in range(3):
        market_data.record_provider_result(
            session, "nasdaq", "history", DataState.FAILED, "timeout"
        )
    row = session.get(ProviderHealthRow, ("nasdaq", "history"))
    assert row is not None
    assert row.consecutive_failures == 3

    market_data.record_provider_result(session, "nasdaq", "history", DataState.HEALTHY)
    assert row.consecutive_failures == 0
    assert row.last_success_at is not None
    # The earlier failure time is kept: "when did this last break" is a
    # different question from "is it broken now".
    assert row.last_failure_at is not None


def test_a_provider_that_has_not_succeeded_recently_reads_as_stale(
    session: Session,
) -> None:
    """Trusting the stored state alone is how a feed that stopped updating
    a week ago keeps showing green."""
    market_data.record_provider_result(session, "nasdaq", "history", DataState.HEALTHY)
    row = session.get(ProviderHealthRow, ("nasdaq", "history"))
    assert row is not None
    assert market_data.provider_health(session)[0]["state"] == "healthy"

    row.last_success_at = row.last_success_at - timedelta(days=30)  # type: ignore[operator]
    session.flush()
    assert market_data.provider_health(session)[0]["state"] == "stale"


# -- findings -----------------------------------------------------------


def test_repeating_a_finding_updates_it_rather_than_duplicating(session: Session) -> None:
    """A problem that persists across fifty runs should be one row with a
    first-seen time, not fifty rows nobody reads."""
    first = market_data.record_finding(
        session, "price_fetch_failed", "timeout", subject_type="symbol", subject_id="ACME"
    )
    second = market_data.record_finding(
        session, "price_fetch_failed", "still timing out",
        subject_type="symbol", subject_id="ACME",
    )
    assert first == second
    findings = market_data.open_findings(session)
    assert len(findings) == 1
    assert findings[0].message == "still timing out"


def test_resolving_keeps_the_history_rather_than_deleting_it(session: Session) -> None:
    """What was once wrong is what makes "has this always been broken?"
    answerable."""
    market_data.record_finding(
        session, "price_fetch_failed", "timeout", subject_type="symbol", subject_id="ACME"
    )
    assert market_data.resolve_findings(session, "price_fetch_failed", "ACME") == 1
    assert market_data.open_findings(session) == []
    # Reopening after a resolve creates a new finding, not a resurrection.
    market_data.record_finding(
        session, "price_fetch_failed", "again", subject_type="symbol", subject_id="ACME"
    )
    assert len(market_data.open_findings(session)) == 1


def test_a_provider_disagreement_becomes_a_readable_finding(session: Session) -> None:
    market_data.record_disagreements(
        session,
        [
            Disagreement(
                subject="ACME",
                metric="price",
                values={"nasdaq": Decimal("10.00"), "yahoo": Decimal("12.00")},
                spread_percent=Decimal("20"),
            )
        ],
    )
    finding = market_data.open_findings(session)[0]
    assert finding.code == "provider_disagreement"
    assert finding.subject_id == "ACME"
    assert "20.00%" in finding.message
    assert "nasdaq=10.00" in finding.message


# -- refresh ------------------------------------------------------------


def test_a_failed_symbol_is_recorded_without_failing_the_batch(
    session: Session, settings: Any, tmp_path: Any
) -> None:
    """A delisted ticker in a watchlist must not stop the rest of a
    portfolio being revalued."""
    cache = PriceCache(
        root=tmp_path / "prices",
        chain=HistoryChain([FakeHistory("fake", ProviderResult.failed("fake", "gone"))]),
    )
    outcomes = market_data.refresh_prices(session, settings, ["ACME", "BETA"], cache=cache)
    assert set(outcomes) == {"ACME", "BETA"}
    assert all(state == "failed" for state in outcomes.values())

    findings = market_data.open_findings(session)
    assert {finding.subject_id for finding in findings} == {"ACME", "BETA"}
    assert all(finding.severity == "error" for finding in findings)


def test_a_recovered_symbol_resolves_its_earlier_finding(
    session: Session, settings: Any, tmp_path: Any
) -> None:
    broken = PriceCache(
        root=tmp_path / "prices",
        chain=HistoryChain([FakeHistory("fake", ProviderResult.failed("fake", "gone"))]),
    )
    market_data.refresh_prices(session, settings, ["ACME"], cache=broken)
    assert len(market_data.open_findings(session)) == 1

    working = PriceCache(
        root=tmp_path / "prices",
        chain=HistoryChain(
            [FakeHistory("fake", ProviderResult.healthy([_bar(date(2026, 1, 2))], None))]
        ),
    )
    market_data.refresh_prices(session, settings, ["ACME"], cache=working, force=True)
    assert market_data.open_findings(session) == []


def test_closing_prices_omit_what_is_unknown_rather_than_zeroing_it(
    tmp_path: Any,
) -> None:
    """A symbol absent from the result is what lets the portfolio engine
    mark its total unknown instead of quietly understating it."""
    cache = PriceCache(
        root=tmp_path / "prices",
        chain=HistoryChain([FakeHistory("fake", ProviderResult.missing("fake"))]),
    )
    cache.store("ACME", [_bar(date(2026, 1, 2))])
    prices, dates = market_data.closing_prices(cache, ["ACME", "NOPE"], on=date(2026, 1, 5))
    assert prices == {"ACME": Decimal("10.5")}
    assert dates == {"ACME": date(2026, 1, 2)}
    assert "NOPE" not in prices


def test_an_unknown_provider_name_degrades_the_chain_instead_of_stopping_boot(
    settings: Any,
) -> None:
    """A typo in configuration should not make the application refuse to
    start; the health page shows which providers are actually in use."""
    settings.prices.provider_priority = ["nasdaq", "bloomberg-terminal"]
    chain = market_data.build_history_chain(settings)
    assert [provider.name for provider in chain.providers] == ["nasdaq"]


def test_a_configuration_naming_no_known_provider_still_yields_a_usable_chain(
    settings: Any,
) -> None:
    settings.prices.provider_priority = ["nothing-real"]
    assert market_data.build_history_chain(settings).providers
