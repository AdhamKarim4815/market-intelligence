"""Portfolio service against a real database built by the real migrations.

The schema under test is the one in `migrations/`, applied by the migration
runner — not one conjured by `create_all`. That is deliberate: a model and a
migration that have drifted apart is precisely the failure this project's
schema discipline exists to prevent, so the tests have to exercise the
migration or they would not catch it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from akmarkets.db.engine import Database
from akmarkets.domain.records import (
    CostBasisMethod,
    PortfolioRole,
    TransactionType,
)
from akmarkets.services import portfolio as service


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


@pytest.fixture
def setup(session: Session) -> dict[str, Any]:
    user = service.get_or_create_user(session, "Owner@Example.com ")
    folio = service.create_portfolio(session, user.user_id, "Long Term")
    acme = service.get_or_create_instrument(session, "acme", name="Acme Corp")
    return {"user": user, "portfolio": folio, "acme": acme}


# -- identity and access ------------------------------------------------


def test_user_email_is_normalised_and_the_id_is_stable(session: Session) -> None:
    """Access supplies an email; the application's identity is the UUID, so
    changing how people authenticate never invalidates their data."""
    first = service.get_or_create_user(session, "  Owner@Example.COM ")
    again = service.get_or_create_user(session, "owner@example.com")
    assert first.email == "owner@example.com"
    assert first.user_id == again.user_id


def test_the_owner_is_an_owner_without_a_membership_row(
    setup: dict[str, Any], session: Session
) -> None:
    """A portfolio can never be orphaned by a bad delete on the member table."""
    role = service.role_for(session, setup["portfolio"].portfolio_id, setup["user"].user_id)
    assert role is PortfolioRole.OWNER


def test_a_stranger_has_no_access(setup: dict[str, Any], session: Session) -> None:
    other = service.get_or_create_user(session, "someone@else.com")
    assert service.role_for(session, setup["portfolio"].portfolio_id, other.user_id) is None
    with pytest.raises(service.AccessDenied):
        service.require_access(session, setup["portfolio"].portfolio_id, other.user_id)


def test_a_viewer_may_read_but_not_write(setup: dict[str, Any], session: Session) -> None:
    viewer = service.get_or_create_user(session, "viewer@example.com")
    service.add_member(
        session, setup["portfolio"].portfolio_id, viewer.user_id, PortfolioRole.VIEWER
    )
    assert (
        service.require_access(session, setup["portfolio"].portfolio_id, viewer.user_id)
        is PortfolioRole.VIEWER
    )
    with pytest.raises(service.AccessDenied):
        service.require_access(
            session, setup["portfolio"].portfolio_id, viewer.user_id, write=True
        )


def test_listing_includes_owned_and_shared_without_duplicates(
    setup: dict[str, Any], session: Session
) -> None:
    owner_id = setup["user"].user_id
    shared = service.create_portfolio(session, owner_id, "Family")
    other = service.get_or_create_user(session, "other@example.com")
    service.add_member(session, shared.portfolio_id, other.user_id, PortfolioRole.VIEWER)
    # The owner is also a member; the row must not appear twice.
    service.add_member(session, shared.portfolio_id, owner_id, PortfolioRole.OWNER)

    assert [p.name for p in service.list_portfolios(session, owner_id)] == [
        "Family",
        "Long Term",
    ]
    assert [p.name for p in service.list_portfolios(session, other.user_id)] == ["Family"]


# -- instruments --------------------------------------------------------


def test_the_same_ticker_on_two_exchanges_is_two_instruments(session: Session) -> None:
    """Uniqueness is on (symbol, exchange). A lookup on the ticker alone
    would eventually merge two different companies into one history."""
    nasdaq = service.get_or_create_instrument(session, "ABC", exchange="NASDAQ")
    london = service.get_or_create_instrument(session, "ABC", exchange="LSE")
    assert nasdaq.instrument_id != london.instrument_id
    assert service.get_or_create_instrument(session, "abc", exchange="NASDAQ").instrument_id == (
        nasdaq.instrument_id
    )


# -- the ledger, through storage ----------------------------------------


def test_decimals_survive_the_round_trip_through_the_database(
    setup: dict[str, Any], session: Session
) -> None:
    """The point of the exact-decimal column types. A price of 13.3300 that
    comes back as 13.329999999999998 would show up as cents of drift in a
    cost basis compared against a broker statement."""
    service.record_transaction(
        session,
        setup["portfolio"].portfolio_id,
        TransactionType.BUY,
        date(2026, 1, 5),
        instrument_id=setup["acme"].instrument_id,
        quantity="33.33333333",
        price="13.3300",
        fees="4.9500",
    )
    stored = service.list_transactions(session, setup["portfolio"].portfolio_id)[0]
    assert stored.quantity == Decimal("33.33333333")
    assert stored.price == Decimal("13.3300")
    assert stored.fees == Decimal("4.9500")
    assert isinstance(stored.price, Decimal)


def test_state_is_derived_from_the_stored_ledger(
    setup: dict[str, Any], session: Session
) -> None:
    """100 @ $10 with $5 fee, then sell 40 @ $15 with $5 fee.

    FIFO basis on the sale is 40×10 + 40/100 of the $5 entry fee = $402.
    Proceeds $600, exit fee $5, so realised gain = 600 − 402 − 5 = $193.
    """
    folio = setup["portfolio"].portfolio_id
    acme = setup["acme"].instrument_id
    service.record_transaction(
        session, folio, TransactionType.BUY, date(2026, 1, 5),
        instrument_id=acme, quantity=100, price=10, fees=5,
    )
    service.record_transaction(
        session, folio, TransactionType.SELL, date(2026, 3, 5),
        instrument_id=acme, quantity=40, price=15, fees=5,
    )
    state = service.portfolio_state(session, folio)
    held = state.positions[0]
    assert held.quantity == Decimal("60")
    assert held.realized_gain == Decimal("193.0000")
    assert state.cash_total == Decimal("-410.0000")


def test_as_of_reads_only_what_had_happened_by_then(
    setup: dict[str, Any], session: Session
) -> None:
    folio = setup["portfolio"].portfolio_id
    acme = setup["acme"].instrument_id
    service.record_transaction(
        session, folio, TransactionType.BUY, date(2026, 1, 5),
        instrument_id=acme, quantity=100, price=10,
    )
    service.record_transaction(
        session, folio, TransactionType.BUY, date(2026, 6, 5),
        instrument_id=acme, quantity=50, price=20,
    )
    january = service.portfolio_state(session, folio, as_of=date(2026, 2, 1))
    assert january.positions[0].quantity == Decimal("100")
    assert service.portfolio_state(session, folio).positions[0].quantity == Decimal("150")


def test_correcting_a_backdated_trade_changes_every_figure_after_it(
    setup: dict[str, Any], session: Session
) -> None:
    """The reason positions are never cached. Editing a three-month-old
    trade must correct the realised gain on a later sale, and it does
    because nothing derived was stored."""
    folio = setup["portfolio"].portfolio_id
    acme = setup["acme"].instrument_id
    buy = service.record_transaction(
        session, folio, TransactionType.BUY, date(2026, 1, 5),
        instrument_id=acme, quantity=100, price=10,
    )
    service.record_transaction(
        session, folio, TransactionType.SELL, date(2026, 3, 5),
        instrument_id=acme, quantity=100, price=15,
    )
    assert service.portfolio_state(session, folio).realized_gain == Decimal("500.0000")

    # The purchase price was actually $12, not $10.
    service.update_transaction(session, buy.model_copy(update={"price": Decimal("12")}))
    assert service.portfolio_state(session, folio).realized_gain == Decimal("300.0000")


def test_deleting_a_transaction_removes_its_effect_entirely(
    setup: dict[str, Any], session: Session
) -> None:
    folio = setup["portfolio"].portfolio_id
    acme = setup["acme"].instrument_id
    keep = service.record_transaction(
        session, folio, TransactionType.BUY, date(2026, 1, 5),
        instrument_id=acme, quantity=100, price=10,
    )
    drop = service.record_transaction(
        session, folio, TransactionType.BUY, date(2026, 2, 5),
        instrument_id=acme, quantity=50, price=20,
    )
    service.delete_transaction(session, drop.transaction_id)
    state = service.portfolio_state(session, folio)
    assert state.positions[0].quantity == Decimal("100")
    assert [t.transaction_id for t in service.list_transactions(session, folio)] == [
        keep.transaction_id
    ]


def test_the_portfolios_configured_method_is_used_by_default(
    session: Session
) -> None:
    """A portfolio set to LIFO must not be silently valued as FIFO."""
    user = service.get_or_create_user(session, "lifo@example.com")
    folio = service.create_portfolio(
        session, user.user_id, "Trading", cost_basis_method=CostBasisMethod.LIFO
    )
    acme = service.get_or_create_instrument(session, "LIFO")
    for on, quantity, price in [("2026-01-05", 100, 10), ("2026-02-05", 50, 20)]:
        service.record_transaction(
            session, folio.portfolio_id, TransactionType.BUY,
            date.fromisoformat(on), instrument_id=acme.instrument_id,
            quantity=quantity, price=price,
        )
    service.record_transaction(
        session, folio.portfolio_id, TransactionType.SELL, date(2026, 3, 5),
        instrument_id=acme.instrument_id, quantity=50, price=30,
    )
    # LIFO consumes the $20 lot: 50×30 − 50×20 = $500. FIFO would give $1,000.
    assert service.portfolio_state(session, folio.portfolio_id).realized_gain == Decimal(
        "500.0000"
    )


def test_validation_surfaces_an_oversell_rather_than_hiding_it(
    setup: dict[str, Any], session: Session
) -> None:
    service.record_transaction(
        session, setup["portfolio"].portfolio_id, TransactionType.SELL, date(2026, 1, 5),
        instrument_id=setup["acme"].instrument_id, quantity=10, price=20,
    )
    findings = service.validate_portfolio(session, setup["portfolio"].portfolio_id)
    assert [finding.code for finding in findings] == ["oversold"]
    assert findings[0].severity == "error"


def test_unknown_ids_raise_not_found(session: Session) -> None:
    with pytest.raises(service.NotFound):
        service.get_portfolio(session, "nope")
    with pytest.raises(service.NotFound):
        service.get_transaction(session, "nope")
