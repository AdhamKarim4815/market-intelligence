-- The AI cost ledger.
--
-- Every call is recorded with the feature that made it, so the question
-- "which features are consuming money" has an answer rather than an
-- estimate. Recorded before the result is used, so a crash mid-response
-- still costs what it cost — accounting that only records successes
-- systematically understates the bill.

create table mi_ai_calls (
    call_id       varchar(36) primary key,
    -- Which product feature spent this. The whole point of the table.
    feature       varchar(60) not null,
    tier          integer     not null default 2,
    model         varchar(60) not null,
    prompt_tokens      integer not null default 0,
    completion_tokens  integer not null default 0,
    -- Stored rather than recomputed at read time: published prices change,
    -- and a historical bill should not move when they do.
    cost_usd      varchar(24) not null default '0',
    subject       varchar(60) not null default '',
    run_id        varchar(36) not null default '',
    created_at    timestamp   not null
);

create index ix_mi_ai_calls_day on mi_ai_calls(created_at);
create index ix_mi_ai_calls_feature on mi_ai_calls(feature, created_at);

--@postgres-only-begin
alter table mi_ai_calls enable row level security;
create policy akmarkets_app_rw on mi_ai_calls
    for all to akmarkets_app using (true) with check (true);
grant select, insert, update, delete on mi_ai_calls to akmarkets_app;
revoke all on mi_ai_calls from anon, authenticated;
--@postgres-only-end
