-- Core portfolio model: users, portfolios, membership, instruments, ledger.
--
-- Runs against Postgres (Supabase, production) and SQLite (local, no cloud
-- credentials required). Statements with no SQLite equivalent -- roles,
-- GRANT, RLS -- are wrapped in postgres-only blocks and stripped before
-- execution on SQLite.
--
-- Money and share quantities are `numeric` on Postgres and `varchar` on
-- SQLite, added in dialect-specific blocks below. That difference is not
-- cosmetic: a column declared `numeric` in SQLite has NUMERIC affinity,
-- which silently converts the exact decimal string we store back into a
-- float and defeats the whole point of akmarkets/db/types.py. All
-- arithmetic on these columns happens in Python `Decimal`; they are never
-- summed or ordered in SQL.

create table mi_users (
    user_id       varchar(36)  primary key,
    email         varchar(320) not null unique,
    display_name  varchar(200) not null default '',
    timezone      varchar(64)  not null default 'UTC',
    is_active     boolean      not null default true,
    created_at    timestamp    not null
);

create table mi_portfolios (
    portfolio_id       varchar(36)  primary key,
    owner_user_id      varchar(36)  not null references mi_users(user_id),
    name               varchar(200) not null,
    base_currency      varchar(3)   not null default 'USD',
    -- A *view* over the ledger, not a property of it. Lots are always kept
    -- individually, so changing this re-derives every figure rather than
    -- migrating any stored value.
    cost_basis_method  varchar(16)  not null default 'fifo',
    benchmark_symbol   varchar(20)  not null default 'SPY',
    is_archived        boolean      not null default false,
    created_at         timestamp    not null
);

create index ix_mi_portfolios_owner on mi_portfolios(owner_user_id);

-- Access is separate from ownership: a shared family portfolio needs the
-- first without changing the second.
create table mi_portfolio_members (
    portfolio_id  varchar(36) not null references mi_portfolios(portfolio_id),
    user_id       varchar(36) not null references mi_users(user_id),
    role          varchar(16) not null default 'viewer',
    created_at    timestamp   not null,
    primary key (portfolio_id, user_id)
);

create index ix_mi_portfolio_members_user on mi_portfolio_members(user_id);

create table mi_instruments (
    instrument_id  varchar(36)  primary key,
    symbol         varchar(20)  not null,
    name           varchar(300) not null default '',
    exchange       varchar(20)  not null default '',
    currency       varchar(3)   not null default 'USD',
    asset_type     varchar(16)  not null default 'equity',
    sector         varchar(100),
    industry       varchar(150),
    country        varchar(2),
    isin           varchar(12),
    cusip          varchar(9),
    -- SEC Central Index Key: the join to EDGAR, which does not know tickers.
    cik            varchar(10),
    is_active      boolean      not null default true,
    created_at     timestamp    not null
);

create index ix_mi_instruments_symbol on mi_instruments(symbol);
create index ix_mi_instruments_cik on mi_instruments(cik);

-- Uniqueness is on (symbol, exchange), never on the ticker alone: tickers
-- are reused after a delisting and differ between exchanges, so a unique
-- index on symbol would eventually refuse a legitimate instrument or, worse,
-- merge two different companies.
create unique index ux_mi_instruments_symbol_exchange on mi_instruments(symbol, exchange);

-- The ledger. Every position, cost basis, realised gain and portfolio value
-- in the application is derived by replaying this table; none of them are
-- stored. That is what makes a corrected backdated trade fix every figure
-- that depended on it instead of leaving a stale cached position behind.
create table mi_transactions (
    transaction_id    varchar(36) primary key,
    portfolio_id      varchar(36) not null references mi_portfolios(portfolio_id),
    instrument_id     varchar(36) references mi_instruments(instrument_id),
    transaction_type  varchar(24) not null,
    -- When it happened in the market.
    occurred_on       date        not null,
    -- When this system learned about it. Backdating makes these differ, and
    -- conflating them would make historical reconstruction wrong.
    recorded_at       timestamp   not null,

    currency          varchar(3)  not null default 'USD',
    note              text        not null default '',
    source            varchar(80) not null default 'manual',
    updated_at        timestamp   not null
);

-- The exact-decimal columns, typed per backend. Positive magnitudes only:
-- direction comes from transaction_type, never from a sign, because one
-- field carrying two meanings produces silent sign errors. `split_ratio` is
-- 3 for a 3-for-1 split and 0.1 for a 1-for-10 reverse split.
--@postgres-only-begin
alter table mi_transactions add column quantity    numeric(28,8) not null default 0;
alter table mi_transactions add column price       numeric(28,4) not null default 0;
alter table mi_transactions add column fees        numeric(28,4) not null default 0;
alter table mi_transactions add column amount      numeric(28,4) not null default 0;
alter table mi_transactions add column split_ratio numeric(28,8);
--@postgres-only-end

--@sqlite-only-begin
alter table mi_transactions add column quantity    varchar(48) not null default '0';
alter table mi_transactions add column price       varchar(48) not null default '0';
alter table mi_transactions add column fees        varchar(48) not null default '0';
alter table mi_transactions add column amount      varchar(48) not null default '0';
alter table mi_transactions add column split_ratio varchar(48);
--@sqlite-only-end

-- The only access pattern that matters for the ledger: one portfolio, in
-- date order, for a replay.
create index ix_mi_transactions_portfolio_date
    on mi_transactions(portfolio_id, occurred_on);
create index ix_mi_transactions_instrument on mi_transactions(instrument_id);

--@postgres-only-begin
-- Nothing in this namespace is reachable through PostgREST. The application
-- connects over Postgres directly as a scoped role; the REST surface would
-- be a second, differently-authorised door onto private portfolio data.
alter table mi_users             enable row level security;
alter table mi_portfolios        enable row level security;
alter table mi_portfolio_members enable row level security;
alter table mi_instruments       enable row level security;
alter table mi_transactions      enable row level security;

revoke all on mi_users             from anon, authenticated;
revoke all on mi_portfolios        from anon, authenticated;
revoke all on mi_portfolio_members from anon, authenticated;
revoke all on mi_instruments       from anon, authenticated;
revoke all on mi_transactions      from anon, authenticated;
revoke all on mi_schema_migrations from anon, authenticated;
--@postgres-only-end
