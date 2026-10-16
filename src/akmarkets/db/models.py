"""ORM models for the `mi_*` tables.

These mirror the migration files; they do not create them. `Base.metadata`
is never used to build a schema — a drift between a model and a migration is
caught by the schema check and by `tests/test_models_match_migrations.py`,
not papered over by `create_all`.

Rows map to and from the frozen records in `akmarkets.domain.records`. The
conversion lives here rather than on the domain types so that the domain
stays free of any storage concept.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from akmarkets.db.base import Base, as_utc, utc_now
from akmarkets.db.types import MoneyColumn, QuantityColumn
from akmarkets.domain.records import (
    AssetType,
    CostBasisMethod,
    Instrument,
    Portfolio,
    PortfolioRole,
    Transaction,
    TransactionType,
    User,
)


class UserRow(Base):
    __tablename__ = "mi_users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_domain(self) -> User:
        return User(
            user_id=self.user_id,
            email=self.email,
            display_name=self.display_name,
            timezone=self.timezone,
            is_active=self.is_active,
            created_at=as_utc(self.created_at),
        )


class PortfolioRow(Base):
    __tablename__ = "mi_portfolios"

    portfolio_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_users.user_id"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    cost_basis_method: Mapped[str] = mapped_column(String(16), default="fifo")
    benchmark_symbol: Mapped[str] = mapped_column(String(20), default="SPY")
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_domain(self) -> Portfolio:
        return Portfolio(
            portfolio_id=self.portfolio_id,
            owner_user_id=self.owner_user_id,
            name=self.name,
            base_currency=self.base_currency,
            cost_basis_method=CostBasisMethod(self.cost_basis_method),
            benchmark_symbol=self.benchmark_symbol,
            is_archived=self.is_archived,
            created_at=as_utc(self.created_at),
        )


class PortfolioMemberRow(Base):
    """Who may see or change a portfolio.

    Separate from `owner_user_id` on purpose: ownership and access are
    different questions, and a shared family portfolio needs the second
    without changing the first.
    """

    __tablename__ = "mi_portfolio_members"

    portfolio_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_portfolios.portfolio_id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_users.user_id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def portfolio_role(self) -> PortfolioRole:
        return PortfolioRole(self.role)


class InstrumentRow(Base):
    """One canonical identity per security.

    `symbol` is indexed but not unique: the same ticker is reused after a
    delisting and differs between exchanges, so uniqueness lives on
    (symbol, exchange) in the migration rather than on the ticker alone.
    """

    __tablename__ = "mi_instruments"

    instrument_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    exchange: Mapped[str] = mapped_column(String(20), default="")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    asset_type: Mapped[str] = mapped_column(String(16), default="equity")
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(150), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    cusip: Mapped[str | None] = mapped_column(String(9), nullable=True)
    # SEC Central Index Key. The join to EDGAR, which does not know tickers.
    cik: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    def to_domain(self) -> Instrument:
        return Instrument(
            instrument_id=self.instrument_id,
            symbol=self.symbol,
            name=self.name,
            exchange=self.exchange,
            currency=self.currency,
            asset_type=AssetType(self.asset_type),
            sector=self.sector,
            industry=self.industry,
            country=self.country,
            isin=self.isin,
            cusip=self.cusip,
            cik=self.cik,
            is_active=self.is_active,
        )


class TransactionRow(Base):
    """The ledger. Append-mostly, and the source of every derived figure.

    A correction rewrites a row rather than adding a reversing entry, but
    `recorded_at` and `source` preserve what happened: positions are always
    re-derived by replaying this table, so an edit to a three-month-old
    trade correctly changes every figure that depended on it.
    """

    __tablename__ = "mi_transactions"

    transaction_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_portfolios.portfolio_id"), index=True
    )
    instrument_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("mi_instruments.instrument_id"), nullable=True, index=True
    )
    transaction_type: Mapped[str] = mapped_column(String(24), index=True)
    # When it happened in the market.
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    # When this system learned about it. Backdated entries make these differ.
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    quantity: Mapped[Decimal] = mapped_column(QuantityColumn(), default=Decimal("0"))
    price: Mapped[Decimal] = mapped_column(MoneyColumn(), default=Decimal("0"))
    fees: Mapped[Decimal] = mapped_column(MoneyColumn(), default=Decimal("0"))
    amount: Mapped[Decimal] = mapped_column(MoneyColumn(), default=Decimal("0"))
    split_ratio: Mapped[Decimal | None] = mapped_column(QuantityColumn(), nullable=True)

    currency: Mapped[str] = mapped_column(String(3), default="USD")
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(80), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        # The replay always reads one portfolio in date order, so this is
        # the only access pattern that matters for the ledger.
        Index("ix_mi_transactions_portfolio_date", "portfolio_id", "occurred_on"),
    )

    def to_domain(self) -> Transaction:
        return Transaction(
            transaction_id=self.transaction_id,
            portfolio_id=self.portfolio_id,
            instrument_id=self.instrument_id,
            transaction_type=TransactionType(self.transaction_type),
            occurred_on=self.occurred_on,
            recorded_at=as_utc(self.recorded_at) or utc_now(),
            quantity=self.quantity,
            price=self.price,
            fees=self.fees,
            amount=self.amount,
            split_ratio=self.split_ratio,
            currency=self.currency,
            note=self.note,
            source=self.source,
        )

    @classmethod
    def from_domain(cls, transaction: Transaction) -> TransactionRow:
        return cls(
            transaction_id=transaction.transaction_id,
            portfolio_id=transaction.portfolio_id,
            instrument_id=transaction.instrument_id,
            transaction_type=transaction.transaction_type.value,
            occurred_on=transaction.occurred_on,
            recorded_at=transaction.recorded_at,
            quantity=transaction.quantity,
            price=transaction.price,
            fees=transaction.fees,
            amount=transaction.amount,
            split_ratio=transaction.split_ratio,
            currency=transaction.currency,
            note=transaction.note,
            source=transaction.source,
            updated_at=utc_now(),
        )


class ProviderHealthRow(Base):
    """What each provider last did, per data category.

    Attempt, success and failure are tracked separately because they answer
    different questions. A provider that failed an hour ago and succeeded a
    minute ago is healthy; one whose last attempt succeeded but whose last
    success was a week ago is stale. Collapsing them into a single
    `last_status` loses both distinctions.
    """

    __tablename__ = "mi_provider_health"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    category: Mapped[str] = mapped_column(String(24), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="unknown")
    message: Mapped[str] = mapped_column(Text, default="")
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DataFindingRow(Base):
    """Something about the data that does not look right.

    Open findings have `resolved_at is null`. A run that finds a subject
    clean resolves its previous findings rather than deleting them, so the
    history of what was once wrong survives — which is what makes it
    possible to answer "has this always been broken?".
    """

    __tablename__ = "mi_data_findings"

    finding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(60), index=True)
    severity: Mapped[str] = mapped_column(String(8), default="warn")
    subject_type: Mapped[str] = mapped_column(String(24), default="")
    subject_id: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[str] = mapped_column(String(36), default="")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_mi_data_findings_open", "severity", "detected_at"),
        Index("ix_mi_data_findings_subject", "subject_type", "subject_id"),
    )


class JobRunRow(Base):
    """One execution of a named job.

    `degraded` is a comma-joined list of stage names rather than a relation:
    it is only ever read whole, for display, and a second table for a field
    nobody queries would be structure without purpose.
    """

    __tablename__ = "mi_job_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    source: Mapped[str] = mapped_column(String(40), default="unknown")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    degraded: Mapped[str] = mapped_column(Text, default="")
    items: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_mi_job_runs_job", "job", "started_at"),
        Index("ix_mi_job_runs_status", "status"),
    )

    @property
    def degraded_stages(self) -> list[str]:
        return [name for name in self.degraded.split(",") if name]


class JobLockRow(Base):
    """Whoever currently owns a job name.

    The primary key is the job, so taking the lock is one insert whose
    conflict is the "someone else has it" answer. A select-then-insert has a
    window between the two that a cron job and a manual run started in the
    same second will find.
    """

    __tablename__ = "mi_job_locks"

    job: Mapped[str] = mapped_column(String(60), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(40), default="unknown")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NewsArticleRow(Base):
    """One collected article.

    Stored so deduplication can span runs: an article seen yesterday must
    not become a new event tonight because the process restarted. A
    collapsed duplicate keeps its row and points at what absorbed it, so
    the count of what was collapsed survives.
    """

    __tablename__ = "mi_news_articles"

    article_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(160), default="")
    source_class: Mapped[str] = mapped_column(String(28), default="other_publication")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    summary: Mapped[str] = mapped_column(Text, default="")
    symbols: Mapped[str] = mapped_column(Text, default="")
    duplicate_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    __table_args__ = (
        Index("ix_mi_news_articles_fingerprint", "fingerprint"),
        Index("ix_mi_news_articles_published", "published_at"),
        Index("ix_mi_news_articles_event", "event_id"),
    )


class NewsEventRow(Base):
    """A cluster of articles about one occurrence."""

    __tablename__ = "mi_news_events"

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key_title: Mapped[str] = mapped_column(Text)
    symbols: Mapped[str] = mapped_column(Text, default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    article_count: Mapped[int] = mapped_column(Integer, default=1)
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    best_source_class: Mapped[str] = mapped_column(String(28), default="other_publication")
    confirmation: Mapped[str] = mapped_column(String(20), default="credible_report")
    dimensions: Mapped[str] = mapped_column(Text, default="")
    sentiment_label: Mapped[str] = mapped_column(String(12), default="neutral")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index("ix_mi_news_events_last_seen", "last_seen"),)


class AlertRow(Base):
    """An event that earned attention for one portfolio.

    Keyed by portfolio rather than globally, because the same event can
    matter to one portfolio and not another — which is the entire point of
    weighting attention by position size.
    """

    __tablename__ = "mi_alerts"

    alert_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_portfolios.portfolio_id"), index=True
    )
    event_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(12), default="info")
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    symbols: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[str] = mapped_column(String(24), default="0")
    explanation: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    muted: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_mi_alerts_inbox", "portfolio_id", "created_at"),
        Index("ix_mi_alerts_event", "event_id"),
    )

    @property
    def is_unread(self) -> bool:
        return self.read_at is None and self.archived_at is None


class WatchlistEntryRow(Base):
    """A security watched without being held.

    Separate from positions on purpose: something you are considering
    deserves attention without the system pretending you own it.
    """

    __tablename__ = "mi_watchlist_entries"

    portfolio_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_portfolios.portfolio_id"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    tier: Mapped[str] = mapped_column(String(2), default="C")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FundamentalRow(Base):
    """One reporting period's filed figures for one instrument.

    The composite key is (instrument, period end, annual) because a Q4
    figure and a full-year figure can share an end date and mean completely
    different things — and because a restatement should update the period
    rather than add a second version of it.
    """

    __tablename__ = "mi_fundamentals"

    instrument_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_instruments.instrument_id"), primary_key=True
    )
    period_end: Mapped[date] = mapped_column(Date, primary_key=True)
    is_annual: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fiscal_period: Mapped[str | None] = mapped_column(String(4), nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="sec-edgar")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    revenue: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    gross_profit: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    operating_income: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    eps_diluted: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    cash: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    total_debt: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    total_assets: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    operating_cash_flow: Mapped[Decimal | None] = mapped_column(
        MoneyColumn(), nullable=True
    )
    capex: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    shares_diluted: Mapped[Decimal | None] = mapped_column(QuantityColumn(), nullable=True)

    __table_args__ = (
        Index("ix_mi_fundamentals_instrument", "instrument_id", "period_end"),
    )


class ThesisRow(Base):
    """Why a position was opened, and what would prove it wrong.

    The invalidation criteria are the point of the table. "The price fell"
    is not a reason to sell; "an assumption I named no longer holds" is, and
    only one of those can be checked later.
    """

    __tablename__ = "mi_theses"

    thesis_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_portfolios.portfolio_id"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(20))
    thesis: Mapped[str] = mapped_column(Text, default="")
    target: Mapped[str] = mapped_column(String(80), default="")
    horizon: Mapped[str] = mapped_column(String(40), default="")
    risks: Mapped[str] = mapped_column(Text, default="")
    invalidation: Mapped[str] = mapped_column(Text, default="")
    opened_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ux_mi_theses_position", "portfolio_id", "symbol", unique=True),
    )

    @property
    def invalidation_criteria(self) -> list[str]:
        return [line.strip() for line in self.invalidation.splitlines() if line.strip()]


class PositionDriverRow(Base):
    """One external factor a position depends on."""

    __tablename__ = "mi_position_drivers"

    portfolio_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_portfolios.portfolio_id"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    driver: Mapped[str] = mapped_column(String(80), primary_key=True)
    direction: Mapped[str] = mapped_column(String(10), default="positive")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DecisionRow(Base):
    """One recorded decision, reviewed on its reasoning rather than its luck.

    `review_note` is filled in by a person, deliberately. An automatic
    verdict would grade the outcome, and the outcome is not the thing being
    judged — a good decision can lose money and a bad one can make it.
    """

    __tablename__ = "mi_decisions"

    decision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mi_portfolios.portfolio_id"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(20), default="")
    action: Mapped[str] = mapped_column(String(20))
    decided_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(Text, default="")
    expected: Mapped[str] = mapped_column(Text, default="")
    horizon: Mapped[str] = mapped_column(String(40), default="")
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index("ix_mi_decisions_portfolio", "portfolio_id", "decided_on"),)


class IntelligenceSnapshotRow(Base):
    """What the platform believed about a security on one date.

    `methodology` is stored with it because a score computed under a
    different version is not comparable, and pretending otherwise is how a
    backtest silently measures two different things.
    """

    __tablename__ = "mi_intelligence_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    as_of: Mapped[date] = mapped_column(Date)
    run_id: Mapped[str] = mapped_column(String(36), default="")
    methodology: Mapped[str] = mapped_column(String(20), default="v1")
    trend: Mapped[str] = mapped_column(String(16), default="unknown")
    rsi_state: Mapped[str] = mapped_column(String(16), default="unknown")
    archetype: Mapped[str] = mapped_column(String(28), default="unknown")
    open_events: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ux_mi_snapshots_symbol_date", "symbol", "as_of", unique=True),
    )


class AiCallRow(Base):
    """One model call, with the feature that made it.

    `cost_usd` is stored rather than recomputed at read time: published
    prices change, and a historical bill should not move when they do.
    Kept as text for the same reason every exact figure here is.
    """

    __tablename__ = "mi_ai_calls"

    call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    feature: Mapped[str] = mapped_column(String(60), index=True)
    tier: Mapped[int] = mapped_column(Integer, default=2)
    model: Mapped[str] = mapped_column(String(60))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[str] = mapped_column(String(24), default="0")
    subject: Mapped[str] = mapped_column(String(60), default="")
    run_id: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_mi_ai_calls_day", "created_at"),
        Index("ix_mi_ai_calls_feature", "feature", "created_at"),
    )


class MacroObservationRow(Base):
    """One value of one macro series on one date.

    Keyed by (series, date) rather than a surrogate id because a series has
    exactly one value per day, and Treasury revises published figures — a
    re-fetch must correct the day rather than store a second version of it
    and leave a reader to guess which is current.
    """

    __tablename__ = "mi_macro_observations"

    series_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    observed_on: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal] = mapped_column(QuantityColumn(), default=Decimal("0"))
    unit: Mapped[str] = mapped_column(String(20), default="percent")
    name: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(40), default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index("ix_mi_macro_observations_date", "observed_on"),)


class EarningsEventRow(Base):
    """A scheduled or reported earnings date.

    Every estimate column is nullable and stays nullable. A company with no
    published consensus has no consensus; a zero would be read as "expected
    to break even", which is a different and false claim.
    """

    __tablename__ = "mi_earnings_events"

    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    reports_on: Mapped[date] = mapped_column(Date, primary_key=True)
    company: Mapped[str] = mapped_column(String(200), default="")
    eps_forecast: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    estimate_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_year_eps: Mapped[Decimal | None] = mapped_column(MoneyColumn(), nullable=True)
    # Raw rather than a boolean, so "we do not know the session" stays
    # distinguishable from "before the open".
    time_of_day: Mapped[str] = mapped_column(String(40), default="")
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(40), default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index("ix_mi_earnings_events_date", "reports_on"),)
