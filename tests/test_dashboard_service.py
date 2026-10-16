"""The overview view model.

The trap this whole module guards against: a dashboard returns 200 whether
it is showing the truth, showing nothing, or showing a plausible number it
invented. Every case here asserts on a fact the page exists to communicate.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from akmarkets.db.engine import Database
from akmarkets.db.models import MacroObservationRow
from akmarkets.domain.records import TransactionType
from akmarkets.services import dashboard as dashboard_service
from akmarkets.services import macro as macro_service
from akmarkets.services import portfolio as portfolio_service


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


@pytest.fixture
def user_id(session: Session) -> str:
    return portfolio_service.get_or_create_user(session, "local@localhost").user_id


@pytest.fixture
def folio(session: Session, user_id: str) -> str:
    return portfolio_service.create_portfolio(session, user_id, "Long Term").portfolio_id


def buy(session: Session, folio: str, symbol: str, qty: str, price: str) -> None:
    instrument = portfolio_service.get_or_create_instrument(session, symbol)
    portfolio_service.record_transaction(
        session,
        folio,
        transaction_type=TransactionType.BUY,
        occurred_on=date(2026, 1, 5),
        instrument_id=instrument.instrument_id,
        quantity=Decimal(qty),
        price=Decimal(price),
    )


def test_a_user_with_no_portfolio_still_produces_a_renderable_view(
    session: Session, settings: Any, user_id: str
) -> None:
    """The empty state is a state, not an error. A view model that raises
    here would make "you have not started yet" indistinguishable from "the
    database is down"."""
    view = dashboard_service.overview(session, settings, user_id)
    assert view.is_empty
    assert view.portfolios == []
    assert view.degraded == ()


def test_an_unpriced_holding_withholds_the_total_rather_than_understating_it(
    session: Session, settings: Any, user_id: str, folio: str
) -> None:
    """A total that silently omits a position looks right and is not.

    No price cache exists in this test, so nothing can be priced — the
    total must be None, never the sum of the parts it happened to know.
    """
    buy(session, folio, "AAPL", "10", "100")
    session.flush()

    view = dashboard_service.overview(session, settings, user_id)
    card = view.portfolios[0]
    assert card.state.unpriced_instrument_ids
    assert card.state.total_value is None
    assert card.state.unrealized_gain is None


def test_a_section_that_raises_is_named_rather_than_rendered_empty(
    session: Session, settings: Any, user_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"We could not read the curve" and "there is no curve" are different
    facts. A table rendered empty for the first reason is a quiet lie."""

    def explode(*_: object, **__: object) -> list[macro_service.RateSnapshot]:
        raise RuntimeError("provider table is gone")

    monkeypatch.setattr(macro_service, "latest_rates", explode)
    view = dashboard_service.overview(session, settings, user_id)

    assert view.rates == []
    assert any(entry.startswith("rates:") for entry in view.degraded)
    # The rest of the page survived the failure.
    assert "RuntimeError" in " ".join(view.degraded)


def test_one_failing_section_does_not_take_the_others_down(
    session: Session, settings: Any, user_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page that would explain an outage must not be its first casualty."""

    def explode(*_: object, **__: object) -> Any:
        raise RuntimeError("nope")

    monkeypatch.setattr(macro_service, "upcoming_earnings", explode)
    view = dashboard_service.overview(session, settings, user_id)

    assert view.earnings == []
    assert len(view.degraded) == 1
    assert isinstance(view.recent_runs, list)
    assert isinstance(view.provider_health, list)


def test_a_rate_change_stays_missing_when_the_history_is_too_short(
    session: Session, settings: Any, user_id: str
) -> None:
    """Answering "where are rates versus a year ago" from three weeks of
    data is not a smaller version of the truth."""
    for day, value in ((date(2026, 7, 20), "4.00"), (date(2026, 8, 3), "4.20")):
        session.add(
            MacroObservationRow(
                series_id="BC_10YEAR",
                observed_on=day,
                value=Decimal(value),
                unit="percent",
                name="US Treasury 10-year par yield",
                source="us-treasury",
                retrieved_at=datetime.now(UTC),
            )
        )
    session.flush()

    view = dashboard_service.overview(
        session, settings, user_id, today=date(2026, 8, 3)
    )
    line = view.rates[0]
    assert line.value == Decimal("4.20")
    assert line.change_1m is None
    assert line.change_1y is None


def test_the_rate_window_is_wide_enough_to_answer_a_one_year_question(
    session: Session, settings: Any, user_id: str
) -> None:
    """The bound on `latest_rates` exists to cap a table scan, not to change
    an answer. Too tight and every 1-year column silently becomes an em
    dash, which reads as missing data rather than as a bad query."""
    for day, value in ((date(2025, 8, 1), "3.80"), (date(2026, 8, 3), "4.20")):
        session.add(
            MacroObservationRow(
                series_id="BC_10YEAR",
                observed_on=day,
                value=Decimal(value),
                unit="percent",
                name="US Treasury 10-year par yield",
                source="us-treasury",
                retrieved_at=datetime.now(UTC),
            )
        )
    session.flush()

    view = dashboard_service.overview(
        session, settings, user_id, today=date(2026, 8, 3)
    )
    assert view.rates[0].change_1y == Decimal("0.40")


def test_the_overview_never_reaches_the_network(
    session: Session, settings: Any, user_id: str, folio: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dashboard that fetched on page load would be one provider outage
    from a page that hangs, and one visit from a rate limit. Prices arrive
    through the refresh pipeline or not at all."""
    from akmarkets.providers import cache as cache_module

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the dashboard attempted a network fetch")

    monkeypatch.setattr(cache_module.PriceCache, "ensure", forbidden)
    buy(session, folio, "AAPL", "10", "100")
    session.flush()

    view = dashboard_service.overview(session, settings, user_id)
    assert view.portfolios  # rendered anyway, with the total withheld


def test_unread_counts_and_the_listed_alerts_agree(
    session: Session, settings: Any, user_id: str, folio: str
) -> None:
    """A badge saying three above a list of read items teaches the reader to
    ignore the badge."""
    view = dashboard_service.overview(session, settings, user_id)
    card = view.portfolios[0]
    assert sum(card.unread.values()) >= len(card.top_alerts)
    assert card.unread_total == sum(card.unread.values())
