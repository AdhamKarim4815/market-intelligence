"""Frozen domain records.

These are the vocabulary the whole application speaks. They are immutable
because a transaction that can be mutated in place is a transaction whose
history cannot be trusted, and the entire point of keeping a transaction log
rather than a running balance is that the history *is* the truth.

Nothing here touches storage. Persistence maps to and from these types; the
calculation engine works only with them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from akmarkets.domain.money import ZERO, to_decimal


class TransactionType(StrEnum):
    """Every event that can change a position or a cash balance.

    Deliberately explicit rather than a signed quantity. `SELL` with a
    negative quantity and `BUY` with a positive one is the same field
    carrying two meanings, and the resulting sign errors are silent.
    """

    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    # A dividend paid in shares rather than cash. Adds quantity at zero
    # cost, which changes average cost even though no money moved.
    DIVIDEND_REINVEST = "dividend_reinvest"
    FEE = "fee"
    INTEREST = "interest"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    # Corporate actions. A split does not change cost basis in total, only
    # per share; getting that wrong silently corrupts every return figure
    # computed after it.
    SPLIT = "split"
    # Shares moved in or out of the portfolio without a trade. Cost basis
    # has to travel with them or the receiving portfolio invents a gain.
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"

    @property
    def affects_position(self) -> bool:
        return self in {
            TransactionType.BUY,
            TransactionType.SELL,
            TransactionType.DIVIDEND_REINVEST,
            TransactionType.SPLIT,
            TransactionType.TRANSFER_IN,
            TransactionType.TRANSFER_OUT,
        }

    @property
    def requires_instrument(self) -> bool:
        return self.affects_position or self is TransactionType.DIVIDEND

    @property
    def is_cash_only(self) -> bool:
        return self in {
            TransactionType.DEPOSIT,
            TransactionType.WITHDRAWAL,
            TransactionType.INTEREST,
        }


class CostBasisMethod(StrEnum):
    FIFO = "fifo"
    LIFO = "lifo"
    AVERAGE = "average"


class AssetType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    COMMODITY = "commodity"
    FX = "fx"
    CRYPTO = "crypto"
    CASH = "cash"


class DecimalModel(BaseModel):
    """Base for records carrying exact amounts.

    `strict=False` with a validator on every Decimal field, rather than
    strict mode: provider JSON hands us floats, and `Decimal(0.1)` is not
    `0.1`. Routing through `to_decimal` is what makes a parsed price equal
    the price that was published.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class Instrument(DecimalModel):
    """One canonical identity for a security.

    `instrument_id` is internal and stable. A ticker is not an identity:
    tickers are reused after a delisting, differ between providers, and
    change on a corporate action. Storing history against a ticker is how a
    portfolio ends up holding the wrong company.
    """

    instrument_id: str
    symbol: str
    name: str = ""
    exchange: str = ""
    currency: str = "USD"
    asset_type: AssetType = AssetType.EQUITY
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    isin: str | None = None
    cusip: str | None = None
    cik: str | None = None
    is_active: bool = True

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class Transaction(DecimalModel):
    """One immutable entry in a portfolio's ledger.

    `occurred_on` is when it happened in the market; `recorded_at` is when
    this system learned about it. Backdated entries and corrections are
    normal and expected, so the two dates genuinely differ and conflating
    them would make historical reconstruction wrong.
    """

    transaction_id: str
    portfolio_id: str
    instrument_id: str | None = None
    transaction_type: TransactionType
    occurred_on: date
    recorded_at: datetime

    # Positive magnitudes. Direction comes from `transaction_type`, never
    # from a sign, so a sign error cannot silently invert a trade.
    quantity: Decimal = ZERO
    price: Decimal = ZERO
    fees: Decimal = ZERO
    # Cash actually moved: proceeds, deposit, dividend received. Derived for
    # trades, given directly for cash events.
    amount: Decimal = ZERO

    # For SPLIT: 3 for a 3-for-1, 0.1 for a 1-for-10 reverse split.
    split_ratio: Decimal | None = None

    currency: str = "USD"
    note: str = ""
    # Free-text provenance: "manual", "csv:fidelity", "correction of <id>".
    source: str = "manual"

    @field_validator("quantity", "price", "fees", "amount", "split_ratio", mode="before")
    @classmethod
    def _exact(cls, value: object) -> object:
        return None if value is None else to_decimal(value)

    @field_validator("quantity", "price", "fees")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < ZERO:
            raise ValueError(
                "quantity, price and fees are magnitudes; direction comes from "
                "transaction_type, not from a sign"
            )
        return value

    @property
    def gross_value(self) -> Decimal:
        return self.quantity * self.price


class Lot(DecimalModel):
    """An open parcel of shares with its own cost and acquisition date.

    Lots exist because `shares × average price` throws away the information
    every later question needs: which shares were sold, what the holding
    period was, and what the realised gain actually is. Average cost is a
    *view* computed from lots, never the stored representation.
    """

    lot_id: str
    instrument_id: str
    opened_on: date
    quantity: Decimal
    cost_per_share: Decimal
    # Fees capitalised into the lot at acquisition, kept separately so the
    # basis can be explained rather than just asserted.
    fees: Decimal = ZERO
    source_transaction_id: str = ""

    @field_validator("quantity", "cost_per_share", "fees", mode="before")
    @classmethod
    def _exact(cls, value: object) -> object:
        return to_decimal(value)

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.cost_per_share + self.fees


class Disposal(DecimalModel):
    """A realised sale, matched against the specific lots it consumed.

    Carries the matched lots so a realised gain can be explained down to
    which parcel it came from, which is what makes the figure auditable
    rather than merely produced.
    """

    disposal_id: str
    instrument_id: str
    occurred_on: date
    quantity: Decimal
    proceeds: Decimal
    cost_basis: Decimal
    fees: Decimal = ZERO
    source_transaction_id: str = ""
    matched_lot_ids: tuple[str, ...] = ()

    @field_validator("quantity", "proceeds", "cost_basis", "fees", mode="before")
    @classmethod
    def _exact(cls, value: object) -> object:
        return to_decimal(value)

    @property
    def realized_gain(self) -> Decimal:
        return self.proceeds - self.cost_basis - self.fees


class Position(DecimalModel):
    """A holding as of a point in time. Entirely derived from transactions.

    Never stored as ground truth. Re-deriving from the ledger is cheap and
    means a corrected backdated trade fixes every figure that depends on it
    rather than leaving a stale cached position behind.
    """

    instrument_id: str
    as_of: date
    quantity: Decimal
    cost_basis: Decimal
    lots: tuple[Lot, ...] = ()
    realized_gain: Decimal = ZERO
    dividends_received: Decimal = ZERO
    fees_paid: Decimal = ZERO

    # Filled in only when a price is available. `None` means unknown, and
    # every downstream figure derived from it is None too rather than zero.
    last_price: Decimal | None = None
    price_as_of: date | None = None

    @property
    def average_cost(self) -> Decimal | None:
        """Cost basis per share, or None for a closed position.

        None rather than zero: a closed position has no average cost, and
        rendering one as $0.00 would read as a free holding.
        """
        if self.quantity == ZERO:
            return None
        return self.cost_basis / self.quantity

    @property
    def market_value(self) -> Decimal | None:
        if self.last_price is None:
            return None
        return self.quantity * self.last_price

    @property
    def unrealized_gain(self) -> Decimal | None:
        value = self.market_value
        if value is None:
            return None
        return value - self.cost_basis

    @property
    def is_open(self) -> bool:
        return self.quantity > ZERO


class CashBalance(DecimalModel):
    currency: str
    amount: Decimal

    @field_validator("amount", mode="before")
    @classmethod
    def _exact(cls, value: object) -> object:
        return to_decimal(value)


class PortfolioState(DecimalModel):
    """Everything derived from one portfolio's ledger as of a date."""

    portfolio_id: str
    as_of: date
    positions: tuple[Position, ...] = ()
    cash: tuple[CashBalance, ...] = ()
    disposals: tuple[Disposal, ...] = ()
    # Instruments whose value could not be determined because no price was
    # available. Surfaced rather than silently excluded from the total: a
    # portfolio value missing a position is worse than one that says so.
    unpriced_instrument_ids: tuple[str, ...] = ()

    @property
    def cash_total(self) -> Decimal:
        return sum((balance.amount for balance in self.cash), start=ZERO)

    @property
    def positions_value(self) -> Decimal | None:
        """Market value of holdings, or None if any open position is unpriced.

        Returning a partial sum would understate the portfolio by exactly
        the positions we could not price, and nothing downstream would know.
        """
        if self.unpriced_instrument_ids:
            return None
        return sum(
            (position.market_value or ZERO for position in self.positions if position.is_open),
            start=ZERO,
        )

    @property
    def total_value(self) -> Decimal | None:
        holdings = self.positions_value
        return None if holdings is None else holdings + self.cash_total

    @property
    def total_cost_basis(self) -> Decimal:
        return sum((position.cost_basis for position in self.positions), start=ZERO)

    @property
    def realized_gain(self) -> Decimal:
        return sum((disposal.realized_gain for disposal in self.disposals), start=ZERO)

    @property
    def unrealized_gain(self) -> Decimal | None:
        if self.unpriced_instrument_ids:
            return None
        return sum(
            (position.unrealized_gain or ZERO for position in self.positions),
            start=ZERO,
        )

    @property
    def open_positions(self) -> tuple[Position, ...]:
        return tuple(position for position in self.positions if position.is_open)


class Portfolio(DecimalModel):
    portfolio_id: str
    owner_user_id: str
    name: str
    base_currency: str = "USD"
    cost_basis_method: CostBasisMethod = CostBasisMethod.FIFO
    benchmark_symbol: str = "SPY"
    is_archived: bool = False
    created_at: datetime | None = None


class PortfolioRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    VIEWER = "viewer"

    @property
    def can_write(self) -> bool:
        return self in {PortfolioRole.OWNER, PortfolioRole.ADMIN}

    @property
    def can_manage_members(self) -> bool:
        return self is PortfolioRole.OWNER


class User(DecimalModel):
    user_id: str
    email: str
    display_name: str = ""
    timezone: str = "UTC"
    is_active: bool = True
    created_at: datetime | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class ValidationFinding(DecimalModel):
    """Something about the data that does not look right.

    Findings are counted and rendered, never used to silently drop a row.
    The product's position is that a visible anomaly beats a clean-looking
    dashboard built on a quiet correction.
    """

    code: str
    severity: str = Field(default="warn", pattern="^(info|warn|error)$")
    message: str
    subject_id: str | None = None
