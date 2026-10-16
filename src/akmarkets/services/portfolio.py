"""Portfolio application service.

The only place the CLI, the web routes and the API go for portfolio data.
It loads rows, converts them to domain records, and hands them to the pure
calculation in `akmarkets.domain.lots`. No arithmetic happens here — this
layer is about assembling inputs and enforcing access, and every figure it
returns was computed by code that can be tested with no database.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from akmarkets.db.base import new_id, utc_now
from akmarkets.db.models import (
    InstrumentRow,
    PortfolioMemberRow,
    PortfolioRow,
    TransactionRow,
    UserRow,
)
from akmarkets.domain.lots import build_state, replay
from akmarkets.domain.records import (
    AssetType,
    CostBasisMethod,
    Instrument,
    Portfolio,
    PortfolioRole,
    PortfolioState,
    Transaction,
    TransactionType,
    User,
    ValidationFinding,
)


class AccessDenied(PermissionError):
    """The user may not see or change this portfolio."""


class NotFound(LookupError):
    """No such portfolio, instrument or transaction."""


# -- users --------------------------------------------------------------


def get_or_create_user(session: Session, email: str, display_name: str = "") -> User:
    """Resolve an identity to a stable internal user.

    Cloudflare Access supplies an email; the application's own identity is
    the UUID here. Every later reference -- portfolio ownership, a deep
    link, a future iOS session -- uses that id, so changing how people
    authenticate never invalidates their data.
    """
    normalized = email.strip().lower()
    row = session.scalar(select(UserRow).where(UserRow.email == normalized))
    if row is None:
        row = UserRow(
            user_id=new_id(),
            email=normalized,
            display_name=display_name or normalized.split("@")[0],
            created_at=utc_now(),
        )
        session.add(row)
        session.flush()
    return row.to_domain()


def get_user(session: Session, user_id: str) -> User:
    row = session.get(UserRow, user_id)
    if row is None:
        raise NotFound(f"no user {user_id}")
    return row.to_domain()


# -- access -------------------------------------------------------------


def role_for(session: Session, portfolio_id: str, user_id: str) -> PortfolioRole | None:
    """The user's role, or None if they have no access at all.

    The owner is always an owner even without a membership row, so a
    portfolio can never be orphaned by a bad delete on the member table.
    """
    portfolio = session.get(PortfolioRow, portfolio_id)
    if portfolio is None:
        raise NotFound(f"no portfolio {portfolio_id}")
    if portfolio.owner_user_id == user_id:
        return PortfolioRole.OWNER
    member = session.get(PortfolioMemberRow, (portfolio_id, user_id))
    return member.portfolio_role if member else None


def require_access(
    session: Session, portfolio_id: str, user_id: str, write: bool = False
) -> PortfolioRole:
    role = role_for(session, portfolio_id, user_id)
    if role is None:
        raise AccessDenied(f"user {user_id} has no access to portfolio {portfolio_id}")
    if write and not role.can_write:
        raise AccessDenied(f"role {role.value} cannot modify portfolio {portfolio_id}")
    return role


# -- portfolios ---------------------------------------------------------


def create_portfolio(
    session: Session,
    owner_user_id: str,
    name: str,
    base_currency: str = "USD",
    cost_basis_method: CostBasisMethod = CostBasisMethod.FIFO,
    benchmark_symbol: str = "SPY",
) -> Portfolio:
    row = PortfolioRow(
        portfolio_id=new_id(),
        owner_user_id=owner_user_id,
        name=name.strip(),
        base_currency=base_currency.upper(),
        cost_basis_method=cost_basis_method.value,
        benchmark_symbol=benchmark_symbol.upper(),
        created_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row.to_domain()


def list_portfolios(session: Session, user_id: str) -> list[Portfolio]:
    """Every portfolio the user owns or has been given access to."""
    owned = select(PortfolioRow).where(PortfolioRow.owner_user_id == user_id)
    shared = (
        select(PortfolioRow)
        .join(
            PortfolioMemberRow,
            PortfolioMemberRow.portfolio_id == PortfolioRow.portfolio_id,
        )
        .where(PortfolioMemberRow.user_id == user_id)
    )
    rows = {
        row.portfolio_id: row
        for row in [*session.scalars(owned).all(), *session.scalars(shared).all()]
    }
    return sorted(
        (row.to_domain() for row in rows.values()),
        key=lambda portfolio: portfolio.name.lower(),
    )


def get_portfolio(session: Session, portfolio_id: str) -> Portfolio:
    row = session.get(PortfolioRow, portfolio_id)
    if row is None:
        raise NotFound(f"no portfolio {portfolio_id}")
    return row.to_domain()


def add_member(
    session: Session, portfolio_id: str, user_id: str, role: PortfolioRole
) -> None:
    existing = session.get(PortfolioMemberRow, (portfolio_id, user_id))
    if existing is not None:
        existing.role = role.value
        return
    session.add(
        PortfolioMemberRow(
            portfolio_id=portfolio_id,
            user_id=user_id,
            role=role.value,
            created_at=utc_now(),
        )
    )


# -- instruments --------------------------------------------------------


def get_or_create_instrument(
    session: Session,
    symbol: str,
    exchange: str = "",
    name: str = "",
    asset_type: AssetType = AssetType.EQUITY,
    currency: str = "USD",
) -> Instrument:
    """Resolve a ticker to a stable instrument id.

    Uniqueness is on (symbol, exchange) rather than the ticker alone.
    Tickers are reused after a delisting and differ between exchanges, so a
    lookup on symbol alone would eventually merge two different companies
    into one position history.
    """
    normalized = symbol.strip().upper()
    row = session.scalar(
        select(InstrumentRow).where(
            InstrumentRow.symbol == normalized, InstrumentRow.exchange == exchange
        )
    )
    if row is None:
        row = InstrumentRow(
            instrument_id=new_id(),
            symbol=normalized,
            exchange=exchange,
            name=name,
            asset_type=asset_type.value,
            currency=currency.upper(),
            created_at=utc_now(),
        )
        session.add(row)
        session.flush()
    elif name and not row.name:
        row.name = name
    return row.to_domain()


def list_instruments(session: Session, instrument_ids: Sequence[str]) -> dict[str, Instrument]:
    if not instrument_ids:
        return {}
    rows = session.scalars(
        select(InstrumentRow).where(InstrumentRow.instrument_id.in_(list(instrument_ids)))
    ).all()
    return {row.instrument_id: row.to_domain() for row in rows}


# -- transactions -------------------------------------------------------


def record_transaction(
    session: Session,
    portfolio_id: str,
    transaction_type: TransactionType,
    occurred_on: date,
    instrument_id: str | None = None,
    quantity: object = 0,
    price: object = 0,
    fees: object = 0,
    amount: object = 0,
    split_ratio: object = None,
    currency: str = "USD",
    note: str = "",
    source: str = "manual",
    transaction_id: str | None = None,
) -> Transaction:
    """Append one entry to the ledger.

    Validation happens in the domain record's own validators, so a bad value
    is rejected identically whether it arrived from a web form, a CSV import
    or the CLI.
    """
    transaction = Transaction(
        transaction_id=transaction_id or str(uuid.uuid4()),
        portfolio_id=portfolio_id,
        instrument_id=instrument_id,
        transaction_type=transaction_type,
        occurred_on=occurred_on,
        recorded_at=utc_now(),
        quantity=quantity,
        price=price,
        fees=fees,
        amount=amount,
        split_ratio=split_ratio,
        currency=currency.upper(),
        note=note,
        source=source,
    )
    session.add(TransactionRow.from_domain(transaction))
    session.flush()
    return transaction


def update_transaction(session: Session, transaction: Transaction) -> Transaction:
    """Correct an existing entry in place.

    A correction rewrites the row rather than adding a reversing entry.
    That is safe *because* nothing is derived and stored: positions are
    recomputed by replaying the ledger, so editing a trade from three months
    ago correctly changes every figure that depended on it. A system that
    cached positions would need a reversing entry to stay consistent, and
    would still show the wrong holding period.
    """
    row = session.get(TransactionRow, transaction.transaction_id)
    if row is None:
        raise NotFound(f"no transaction {transaction.transaction_id}")
    updated = TransactionRow.from_domain(transaction)
    for column in TransactionRow.__table__.columns:
        if column.name != "transaction_id":
            setattr(row, column.name, getattr(updated, column.name))
    row.updated_at = utc_now()
    session.flush()
    return transaction


def delete_transaction(session: Session, transaction_id: str) -> None:
    session.execute(
        delete(TransactionRow).where(TransactionRow.transaction_id == transaction_id)
    )


def get_transaction(session: Session, transaction_id: str) -> Transaction:
    row = session.get(TransactionRow, transaction_id)
    if row is None:
        raise NotFound(f"no transaction {transaction_id}")
    return row.to_domain()


def list_transactions(
    session: Session,
    portfolio_id: str,
    instrument_id: str | None = None,
    until: date | None = None,
) -> list[Transaction]:
    query = select(TransactionRow).where(TransactionRow.portfolio_id == portfolio_id)
    if instrument_id is not None:
        query = query.where(TransactionRow.instrument_id == instrument_id)
    if until is not None:
        query = query.where(TransactionRow.occurred_on <= until)
    query = query.order_by(TransactionRow.occurred_on, TransactionRow.transaction_id)
    return [row.to_domain() for row in session.scalars(query).all()]


# -- derived state ------------------------------------------------------


def portfolio_state(
    session: Session,
    portfolio_id: str,
    as_of: date | None = None,
    prices: dict[str, Decimal] | None = None,
    price_dates: dict[str, date] | None = None,
    method: CostBasisMethod | None = None,
) -> PortfolioState:
    """Derive everything about a portfolio from its ledger.

    Recomputed on every call rather than cached. A portfolio of this size
    replays in well under a millisecond, and caching would reintroduce
    exactly the staleness that keeping a transaction log was meant to avoid.
    """
    portfolio = get_portfolio(session, portfolio_id)
    transactions = list_transactions(session, portfolio_id, until=as_of)
    return build_state(
        portfolio_id=portfolio_id,
        transactions=transactions,
        method=method or portfolio.cost_basis_method,
        as_of=as_of,
        prices=prices,
        price_dates=price_dates,
        base_currency=portfolio.base_currency,
    )


def validate_portfolio(
    session: Session, portfolio_id: str, method: CostBasisMethod | None = None
) -> list[ValidationFinding]:
    """Everything about this ledger that does not look right.

    Findings are surfaced, never used to silently drop a row: a portfolio
    reconstructed from partial history routinely has a sale with no matching
    purchase, and a dashboard that quietly corrected that would be lying.
    """
    portfolio = get_portfolio(session, portfolio_id)
    transactions = list_transactions(session, portfolio_id)
    return replay(transactions, method=method or portfolio.cost_basis_method).findings
