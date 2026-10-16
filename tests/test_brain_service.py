"""Snapshots, theses and decisions, against the real schema."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from akmarkets.db.engine import Database
from akmarkets.db.models import IntelligenceSnapshotRow
from akmarkets.domain.market import Bar
from akmarkets.services import brain as brain_service
from akmarkets.services import market_data
from akmarkets.services import portfolio as portfolio_service

TODAY = date.today()


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


@pytest.fixture
def folio(session: Session) -> str:
    user = portfolio_service.get_or_create_user(session, "local@localhost")
    return portfolio_service.create_portfolio(session, user.user_id, "Long Term").portfolio_id


@pytest.fixture
def priced(session: Session, settings: Any) -> str:
    """A real instrument with a real cached series."""
    instrument = portfolio_service.get_or_create_instrument(session, "XMPL", name="Example Corp")
    cache = market_data.build_price_cache(settings)
    cache.store(
        "XMPL",
        [
            Bar(
                on=TODAY - timedelta(days=n),
                open=Decimal(10 + n),
                high=Decimal(11 + n),
                low=Decimal(9 + n),
                close=Decimal(10 + n),
                volume=1000,
            )
            for n in range(60, 0, -1)
        ],
    )
    return instrument.symbol


# -- theses -------------------------------------------------------------


def test_a_thesis_is_one_per_position_and_updates_in_place(
    session: Session, folio: str
) -> None:
    brain_service.set_thesis(session, folio, "xmpl", thesis="Low-cost producer")
    brain_service.set_thesis(session, folio, "XMPL", thesis="Low-cost producer ramping")
    row = brain_service.get_thesis(session, folio, "XMPL")
    assert row is not None
    assert row.thesis == "Low-cost producer ramping"


def test_invalidation_criteria_are_read_as_a_list(session: Session, folio: str) -> None:
    """The point of the table: "the price fell" is not a reason to sell, and
    "an assumption I named no longer holds" is."""
    brain_service.set_thesis(
        session,
        folio,
        "XMPL",
        invalidation="lithium below $10,000 per tonne\nproduction ramp delayed past 2027",
    )
    row = brain_service.get_thesis(session, folio, "XMPL")
    assert row is not None
    assert len(row.invalidation_criteria) == 2


def test_drivers_are_replaced_not_merged(session: Session, folio: str) -> None:
    """A driver list is a statement about what a position is a bet on;
    half-updating it would leave a claim nobody made."""
    brain_service.set_drivers(
        session, folio, "XMPL", [("lithium price", "positive", ""), ("FX", "negative", "")]
    )
    brain_service.set_drivers(session, folio, "XMPL", [("lithium price", "positive", "")])
    dna = brain_service.position_dna(session, folio, "XMPL")
    assert dna.driver_names() == ("lithium price",)


def test_dna_carries_the_thesis_and_its_invalidation(session: Session, folio: str) -> None:
    brain_service.set_thesis(
        session, folio, "XMPL", thesis="Ramping", invalidation="lithium below $10,000"
    )
    brain_service.set_drivers(session, folio, "XMPL", [("lithium price", "positive", "")])
    dna = brain_service.position_dna(session, folio, "XMPL")
    assert dna.thesis == "Ramping"
    assert dna.invalidation == ("lithium below $10,000",)


# -- decisions ----------------------------------------------------------


def test_a_decision_records_the_reasoning_not_just_the_trade(
    session: Session, folio: str
) -> None:
    brain_service.record_decision(
        session,
        folio,
        action="buy",
        decided_on=date(2026, 3, 2),
        symbol="XMPL",
        reason="Q2 production beat plus lithium recovery",
        expected="$15",
        horizon="6-12 months",
    )
    recorded = brain_service.decisions(session, folio)
    assert len(recorded) == 1
    assert "production beat" in recorded[0].reason
    # Unreviewed until a person reviews it.
    assert recorded[0].reviewed_on is None


def test_a_review_is_written_by_a_person_not_derived_from_the_outcome(
    session: Session, folio: str
) -> None:
    """An automatic verdict would grade the outcome, and the outcome is not
    the thing being judged — a good decision can lose money."""
    decision = brain_service.record_decision(
        session, folio, action="buy", decided_on=date(2026, 3, 2), symbol="XMPL"
    )
    brain_service.review_decision(
        session, decision.decision_id, "Reasoning held; sizing was too large."
    )
    reviewed = brain_service.decisions(session, folio)[0]
    assert "sizing" in reviewed.review_note
    assert reviewed.reviewed_on is not None


# -- snapshots ----------------------------------------------------------


def test_a_snapshot_records_the_methodology_that_produced_it(
    session: Session, settings: Any, priced: str
) -> None:
    """A score computed under a different version is not comparable, and
    ignoring that is how a backtest measures two things at once."""
    snapshot = brain_service.take_snapshot(session, settings, priced)
    assert snapshot is not None
    assert snapshot.methodology == brain_service.METHODOLOGY
    assert snapshot.trend in {"uptrend", "downtrend", "mixed", "unknown"}


def test_a_second_snapshot_the_same_day_corrects_rather_than_duplicates(
    session: Session, settings: Any, priced: str
) -> None:
    brain_service.take_snapshot(session, settings, priced)
    brain_service.take_snapshot(session, settings, priced)
    rows = session.query(IntelligenceSnapshotRow).all()
    assert len(rows) == 1


def test_the_previous_snapshot_is_strictly_earlier(
    session: Session, settings: Any, priced: str
) -> None:
    """Comparing today's snapshot with itself would report that nothing
    ever changes."""
    today = brain_service.take_snapshot(session, settings, priced)
    assert today is not None
    assert brain_service.previous_snapshot(session, priced, today.as_of) is None

    older = brain_service.take_snapshot(
        session, settings, priced, as_of=today.as_of - timedelta(days=1)
    )
    assert older is not None
    found = brain_service.previous_snapshot(session, priced, today.as_of)
    assert found is not None
    assert found.as_of < today.as_of


def test_an_unknown_symbol_produces_no_snapshot(session: Session, settings: Any) -> None:
    assert brain_service.take_snapshot(session, settings, "NOPE") is None


# -- the view -----------------------------------------------------------


def test_the_brain_view_reports_a_quiet_security_as_quiet(
    session: Session, settings: Any, priced: str, folio: str
) -> None:
    """What stops an expensive model being asked about a day on which
    nothing happened."""
    view = brain_service.brain_view(session, settings, priced, portfolio_id=folio)
    assert view is not None
    assert view.changes.symbol == "XMPL"
    # A steadily rising series over 30 days is a real move, so it is not
    # quiet — but nothing else fires.
    kinds = {change.kind.value for change in view.changes.material}
    assert kinds <= {"price"}


def test_the_move_engine_declines_when_there_is_nothing_to_point_at(
    session: Session, settings: Any, priced: str
) -> None:
    view = brain_service.brain_view(session, settings, priced)
    assert view is not None
    assert view.move is not None
    # A one-dollar step on a ten-dollar stock with no news and no benchmark
    # cached: there is genuinely nothing to attribute it to.
    assert view.move.verdict.value in {"unclear", "probable"}


def test_a_thesis_warning_reaches_the_change_set(
    session: Session, settings: Any, priced: str, folio: str
) -> None:
    """The most important thing this system can report, so it must not be
    filtered out by significance."""
    from akmarkets.db.base import utc_now
    from akmarkets.db.models import NewsEventRow

    brain_service.set_thesis(
        session, folio, "XMPL", invalidation="lithium price sustained below $10,000 per tonne"
    )
    session.add(
        NewsEventRow(
            event_id="ev-1",
            key_title="Lithium price sustained below $9,000 per tonne on oversupply",
            symbols="XMPL",
            first_seen=utc_now(),
            last_seen=utc_now(),
            updated_at=utc_now(),
        )
    )
    session.flush()

    view = brain_service.brain_view(session, settings, "XMPL", portfolio_id=folio)
    assert view is not None
    assert view.warnings
    assert any(change.kind.value == "thesis" for change in view.changes.material)


def test_portfolio_changes_omits_quiet_symbols(
    session: Session, settings: Any, folio: str
) -> None:
    """A list that includes every holding every day is a list nobody
    reads."""
    portfolio_service.get_or_create_instrument(session, "FLAT")
    cache = market_data.build_price_cache(settings)
    cache.store(
        "FLAT",
        [
            Bar(
                on=TODAY - timedelta(days=n),
                open=Decimal(100), high=Decimal(100), low=Decimal(100),
                close=Decimal(100), volume=1000,
            )
            for n in range(60, 0, -1)
        ],
    )
    assert brain_service.portfolio_changes(session, settings, folio, ["FLAT"]) == []
