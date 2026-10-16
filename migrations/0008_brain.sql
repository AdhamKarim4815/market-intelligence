-- Thesis, position drivers, decisions, and stored intelligence snapshots.
--
-- These four tables are what make the platform's judgements accountable
-- over time. Everything else is derived and can be recomputed; this is the
-- part a person wrote down, and losing it would lose the reasoning.

-- Why a position was opened, and what would prove it wrong. The
-- invalidation criteria are the point: "the price fell" is not a reason to
-- sell, and "an assumption I named no longer holds" is.
create table mi_theses (
    thesis_id     varchar(36) primary key,
    portfolio_id  varchar(36) not null references mi_portfolios(portfolio_id),
    symbol        varchar(20) not null,
    thesis        text        not null default '',
    target        text        not null default '',
    horizon       varchar(40) not null default '',
    risks         text        not null default '',
    -- Newline separated. Read whole, never queried by element.
    invalidation  text        not null default '',
    opened_on     date,
    created_at    timestamp   not null,
    updated_at    timestamp   not null
);

create unique index ux_mi_theses_position on mi_theses(portfolio_id, symbol);

-- What a position is actually a bet on. Written down so the system can
-- recognise that a lithium price move is news for a lithium producer even
-- when the article never names the company.
create table mi_position_drivers (
    portfolio_id  varchar(36) not null references mi_portfolios(portfolio_id),
    symbol        varchar(20) not null,
    driver        varchar(80) not null,
    direction     varchar(10) not null default 'positive',
    note          text        not null default '',
    created_at    timestamp   not null,
    primary key (portfolio_id, symbol, driver)
);

-- The decision journal. Evaluated later on whether the reasoning was sound,
-- not merely on whether the stock went up — which is the only way to learn
-- anything from a sample this small.
create table mi_decisions (
    decision_id   varchar(36) primary key,
    portfolio_id  varchar(36) not null references mi_portfolios(portfolio_id),
    symbol        varchar(20) not null default '',
    action        varchar(20) not null,
    decided_on    date        not null,
    reason        text        not null default '',
    expected      text        not null default '',
    horizon       varchar(40) not null default '',
    -- Filled in later, by a person, on purpose. An automatic verdict would
    -- grade the outcome, and the outcome is not the thing being judged.
    review_note   text        not null default '',
    reviewed_on   date,
    created_at    timestamp   not null
);

create index ix_mi_decisions_portfolio on mi_decisions(portfolio_id, decided_on);

-- A snapshot of what the platform believed about a security on a date, so
-- "why did it rate XMPL that way in August" is answerable. Without this,
-- generated intelligence becomes opaque historical text.
create table mi_intelligence_snapshots (
    snapshot_id   varchar(36) primary key,
    symbol        varchar(20) not null,
    as_of         date        not null,
    run_id        varchar(36) not null default '',
    -- The methodology that produced it. A score computed under a different
    -- version is not comparable, and pretending otherwise is how a
    -- backtest silently measures two different things.
    methodology   varchar(20) not null default 'v1',
    trend         varchar(16) not null default 'unknown',
    rsi_state     varchar(16) not null default 'unknown',
    archetype     varchar(28) not null default 'unknown',
    open_events   integer     not null default 0,
    summary       text        not null default '',
    created_at    timestamp   not null
);

create unique index ux_mi_snapshots_symbol_date on mi_intelligence_snapshots(symbol, as_of);

--@postgres-only-begin
alter table mi_theses                 enable row level security;
alter table mi_position_drivers       enable row level security;
alter table mi_decisions              enable row level security;
alter table mi_intelligence_snapshots enable row level security;

create policy akmarkets_app_rw on mi_theses
    for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_position_drivers
    for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_decisions
    for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_intelligence_snapshots
    for all to akmarkets_app using (true) with check (true);

grant select, insert, update, delete on mi_theses to akmarkets_app;
grant select, insert, update, delete on mi_position_drivers to akmarkets_app;
grant select, insert, update, delete on mi_decisions to akmarkets_app;
grant select, insert, update, delete on mi_intelligence_snapshots to akmarkets_app;

revoke all on mi_theses from anon, authenticated;
revoke all on mi_position_drivers from anon, authenticated;
revoke all on mi_decisions from anon, authenticated;
revoke all on mi_intelligence_snapshots from anon, authenticated;
--@postgres-only-end
