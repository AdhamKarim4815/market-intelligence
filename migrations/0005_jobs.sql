-- Background job runs, with a lock that actually prevents overlap.
--
-- Work reaches this system from four places -- GitHub Actions, a launchd
-- agent, the web app, and someone at a terminal -- and two of them running
-- the same refresh at once is not hypothetical. The lock is a row, not a
-- file or an in-process mutex, because those four processes share nothing
-- except the database.

create table mi_job_runs (
    run_id        varchar(36) primary key,
    job           varchar(60) not null,
    -- running | done | error | abandoned
    status        varchar(16) not null default 'running',
    -- Which of the four environments started it. When two runs collide,
    -- the first question is always "who else is running this".
    source        varchar(40) not null default 'unknown',
    started_at    timestamp   not null,
    finished_at   timestamp,
    -- Refreshed while work is in progress. A crashed process cannot clear
    -- its own lock, so a stale heartbeat is what lets the next run take
    -- over rather than waiting forever on a job that will never finish.
    heartbeat_at  timestamp,
    message       text        not null default '',
    error         text,
    -- Stages that failed while the run continued. A run that finished with
    -- two of nine stages degraded is not a failed run, and calling it one
    -- trains people to ignore the status.
    degraded      text        not null default '',
    items         integer     not null default 0
);

create index ix_mi_job_runs_job on mi_job_runs(job, started_at);
create index ix_mi_job_runs_status on mi_job_runs(status);

-- One row per job name, holding whichever run currently owns it. Separate
-- from mi_job_runs so the lock can be taken with a single insert whose
-- primary key conflict *is* the "someone else has it" answer -- no
-- select-then-insert race.
create table mi_job_locks (
    job         varchar(60) primary key,
    run_id      varchar(36) not null,
    source      varchar(40) not null default 'unknown',
    acquired_at timestamp   not null,
    heartbeat_at timestamp  not null
);

--@postgres-only-begin
alter table mi_job_runs  enable row level security;
alter table mi_job_locks enable row level security;

create policy akmarkets_app_rw on mi_job_runs
    for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_job_locks
    for all to akmarkets_app using (true) with check (true);

grant select, insert, update, delete on mi_job_runs  to akmarkets_app;
grant select, insert, update, delete on mi_job_locks to akmarkets_app;

revoke all on mi_job_runs  from anon, authenticated;
revoke all on mi_job_locks from anon, authenticated;
--@postgres-only-end
