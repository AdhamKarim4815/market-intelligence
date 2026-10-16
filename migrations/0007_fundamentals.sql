-- Filed fundamentals, normalised into periods.
--
-- Stored rather than recomputed from the provider each time because EDGAR
-- rate-limits to ten requests a second and a full company-facts response is
-- megabytes; re-fetching to render a page would be both slow and rude.
--
-- One row per (instrument, period end, annual/quarterly). The composite key
-- is what makes a restatement an update rather than a duplicate.

create table mi_fundamentals (
    instrument_id  varchar(36) not null references mi_instruments(instrument_id),
    period_end     date        not null,
    is_annual      boolean     not null,
    fiscal_year    integer,
    fiscal_period  varchar(4),
    -- The figures, as filed. Stored as text on SQLite and numeric on
    -- Postgres for the same reason as every other exact figure here.
    source         varchar(40) not null default 'sec-edgar',
    retrieved_at   timestamp   not null,
    primary key (instrument_id, period_end, is_annual)
);

create index ix_mi_fundamentals_instrument on mi_fundamentals(instrument_id, period_end);

--@postgres-only-begin
alter table mi_fundamentals add column revenue             numeric(28,4);
alter table mi_fundamentals add column gross_profit        numeric(28,4);
alter table mi_fundamentals add column operating_income    numeric(28,4);
alter table mi_fundamentals add column net_income          numeric(28,4);
alter table mi_fundamentals add column eps_diluted         numeric(28,4);
alter table mi_fundamentals add column cash                numeric(28,4);
alter table mi_fundamentals add column total_debt          numeric(28,4);
alter table mi_fundamentals add column total_assets        numeric(28,4);
alter table mi_fundamentals add column total_equity        numeric(28,4);
alter table mi_fundamentals add column operating_cash_flow numeric(28,4);
alter table mi_fundamentals add column capex               numeric(28,4);
alter table mi_fundamentals add column shares_diluted      numeric(28,8);
--@postgres-only-end

--@sqlite-only-begin
alter table mi_fundamentals add column revenue             varchar(48);
alter table mi_fundamentals add column gross_profit        varchar(48);
alter table mi_fundamentals add column operating_income    varchar(48);
alter table mi_fundamentals add column net_income          varchar(48);
alter table mi_fundamentals add column eps_diluted         varchar(48);
alter table mi_fundamentals add column cash                varchar(48);
alter table mi_fundamentals add column total_debt          varchar(48);
alter table mi_fundamentals add column total_assets        varchar(48);
alter table mi_fundamentals add column total_equity        varchar(48);
alter table mi_fundamentals add column operating_cash_flow varchar(48);
alter table mi_fundamentals add column capex               varchar(48);
alter table mi_fundamentals add column shares_diluted      varchar(48);
--@sqlite-only-end

--@postgres-only-begin
alter table mi_fundamentals enable row level security;
create policy akmarkets_app_rw on mi_fundamentals
    for all to akmarkets_app using (true) with check (true);
grant select, insert, update, delete on mi_fundamentals to akmarkets_app;
revoke all on mi_fundamentals from anon, authenticated;
--@postgres-only-end
