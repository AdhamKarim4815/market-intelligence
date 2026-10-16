-- Provider health and data-quality findings.
--
-- Two tables, both existing to serve the same rule: the system must never
-- let stale or absent data masquerade as current data. One records what
-- each provider last did; the other records everything that did not look
-- right, so an anomaly is counted and rendered rather than quietly dropped.
--
-- Prices themselves are deliberately *not* here. Daily bars are large,
-- never change once a session has settled, and are only ever read as a
-- whole series — which makes them a columnar file under var/, not rows in
-- an operational database. Losing that
-- directory costs time, not data.

create table mi_provider_health (
    provider      varchar(40) not null,
    -- quotes | history | fundamentals | filings | news | macro
    category      varchar(24) not null,
    -- healthy | delayed | degraded | stale | failed | unknown
    state         varchar(16) not null default 'unknown',
    message       text        not null default '',
    -- Separated on purpose: a provider that failed an hour ago but
    -- succeeded a minute ago is healthy, and one that has not succeeded in
    -- a week is not, whatever its last attempt did.
    last_attempt_at timestamp,
    last_success_at timestamp,
    last_failure_at timestamp,
    consecutive_failures integer not null default 0,
    updated_at    timestamp   not null,
    primary key (provider, category)
);

-- Anything about the data that does not look right: a provider
-- disagreement, an implausible move, a series with a hole in it, a sale
-- with no matching purchase. Findings are surfaced and counted, never used
-- to silently correct a row.
create table mi_data_findings (
    finding_id   varchar(36) primary key,
    code         varchar(60) not null,
    -- info | warn | error
    severity     varchar(8)  not null default 'warn',
    subject_type varchar(24) not null default '',
    subject_id   varchar(64) not null default '',
    message      text        not null default '',
    -- Which run produced it, so a finding can be traced to the job that
    -- found it and cleared when that job next runs clean.
    run_id       varchar(36) not null default '',
    detected_at  timestamp   not null,
    resolved_at  timestamp
);

create index ix_mi_data_findings_open
    on mi_data_findings(severity, detected_at);
create index ix_mi_data_findings_subject
    on mi_data_findings(subject_type, subject_id);

--@postgres-only-begin
alter table mi_provider_health enable row level security;
alter table mi_data_findings   enable row level security;

create policy akmarkets_app_rw on mi_provider_health
    for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_data_findings
    for all to akmarkets_app using (true) with check (true);

grant select, insert, update, delete on mi_provider_health to akmarkets_app;
grant select, insert, update, delete on mi_data_findings   to akmarkets_app;

revoke all on mi_provider_health from anon, authenticated;
revoke all on mi_data_findings   from anon, authenticated;
--@postgres-only-end
