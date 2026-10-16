-- Macro rates and the earnings calendar.
--
-- Both arrive from keyless official sources (US Treasury, Nasdaq), so
-- there is no credential to store and nothing here is licensed data.
--
-- Two tables rather than one general "time series" table. A macro
-- observation is a value on a date for a series; an earnings event is a
-- scheduled *future* thing that gets revised and then confirmed. Forcing
-- them into one shape would mean a nullable column for every field only
-- one of them has, and a revision rule that only makes sense for one.
--
-- The CREATE statements are written out once per dialect rather than
-- shared with per-dialect ALTERs. The decimal columns are NOT NULL, and
-- SQLite refuses to add a NOT NULL column without a default even to an
-- empty table — while a default of `0` for a yield is exactly the
-- imputation this project forbids. Duplicating the statement keeps both
-- dialects honest; sharing it would mean weakening one of them.

--@postgres-only-begin
create table mi_macro_observations (
    -- Composite natural key: a series has exactly one value per date, and
    -- a re-fetch of an already-stored day must update rather than
    -- duplicate. Treasury revises published figures.
    series_id     varchar(60)   not null,
    observed_on   date          not null,
    -- Matches QuantityColumn. Basis points are the unit these are argued
    -- in, so the scale is generous and nothing is ever a float.
    value         numeric(28,8) not null,
    unit          varchar(20)   not null default 'percent',
    name          varchar(120)  not null default '',
    source        varchar(40)   not null default '',
    retrieved_at  timestamp     not null,
    primary key (series_id, observed_on)
);

create table mi_earnings_events (
    -- One row per company per reporting date. A company reports once on a
    -- given day; a re-fetch revises the estimate rather than adding a row.
    symbol        varchar(24)  not null,
    reports_on    date         not null,
    company       varchar(200) not null default '',
    -- Nullable throughout, and deliberately so: a company with no
    -- published consensus has no consensus, and a zero here would be read
    -- as "expected to break even", which is a different claim.
    eps_forecast   numeric(28,4) null,
    estimate_count integer       null,
    last_year_eps  numeric(28,4) null,
    -- '', 'time-not-supplied', 'time-pre-market', 'time-after-hours'.
    -- Stored raw rather than as a boolean so the distinction between "we
    -- do not know the session" and "before the open" survives.
    time_of_day   varchar(40) not null default '',
    is_confirmed  boolean     not null default false,
    source        varchar(40) not null default '',
    retrieved_at  timestamp   not null,
    primary key (symbol, reports_on)
);
--@postgres-only-end

--@sqlite-only-begin
create table mi_macro_observations (
    series_id     varchar(60)  not null,
    observed_on   date         not null,
    -- Text on SQLite. NUMERIC affinity coerces a stored decimal string
    -- back through a float on the way out, which is the whole reason the
    -- exact-decimal column types exist.
    value         varchar(48)  not null,
    unit          varchar(20)  not null default 'percent',
    name          varchar(120) not null default '',
    source        varchar(40)  not null default '',
    retrieved_at  timestamp    not null,
    primary key (series_id, observed_on)
);

create table mi_earnings_events (
    symbol        varchar(24)  not null,
    reports_on    date         not null,
    company       varchar(200) not null default '',
    eps_forecast   varchar(48) null,
    estimate_count integer     null,
    last_year_eps  varchar(48) null,
    time_of_day   varchar(40) not null default '',
    is_confirmed  boolean     not null default false,
    source        varchar(40) not null default '',
    retrieved_at  timestamp   not null,
    primary key (symbol, reports_on)
);
--@sqlite-only-end

create index ix_mi_macro_observations_date on mi_macro_observations(observed_on);
create index ix_mi_earnings_events_date on mi_earnings_events(reports_on);

--@postgres-only-begin
alter table mi_macro_observations enable row level security;
create policy akmarkets_app_rw on mi_macro_observations
    for all to akmarkets_app using (true) with check (true);
grant select, insert, update, delete on mi_macro_observations to akmarkets_app;
revoke all on mi_macro_observations from anon, authenticated;

alter table mi_earnings_events enable row level security;
create policy akmarkets_app_rw on mi_earnings_events
    for all to akmarkets_app using (true) with check (true);
grant select, insert, update, delete on mi_earnings_events to akmarkets_app;
revoke all on mi_earnings_events from anon, authenticated;
--@postgres-only-end
