-- Least-privilege runtime role, RLS, and PostgREST lockout.
--
-- Entirely Postgres-only: SQLite has no roles, no GRANT and no row-level
-- security, and local development is single-user against a file. The block
-- markers strip all of it there.
--
-- This file exists so that the runtime role's privileges are versioned with
-- the code. Privileges applied by hand in a SQL editor survive nowhere in a
-- repository, and nobody can then say what a role is actually granted
-- without querying the database. That is not a state to reproduce.
--
-- The role is created NOLOGIN and without a password on purpose. A password
-- must never enter version control, so it is set out of band:
--
--     alter role akmarkets_app with login password '<generated>';
--
-- See DEPLOY.md. Until that runs, the role exists and is correctly scoped
-- but cannot connect, which is the safe order to do this in.

--@postgres-only-begin

-- Idempotent: this migration may be re-run against a database where an
-- earlier attempt created the role.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'akmarkets_app') then
        create role akmarkets_app nologin noinherit;
    end if;
end
$$;

-- Explicitly *not* granted: superuser, BYPASSRLS, CREATEROLE, CREATEDB,
-- and any DDL. The application cannot alter its own schema, which is what
-- makes "startup never migrates" enforceable by the database rather than by
-- convention alone.
--
-- This verifies rather than sets. `alter role ... nosuperuser` is refused
-- on Supabase, whose `postgres` is not itself a superuser and so may not
-- touch the SUPERUSER attribute at all. `create role` above grants none of
-- these by default, so asserting is both permitted and stronger than
-- setting: it fails loudly if a later hand-edit in the SQL editor ever
-- escalates the role, which setting once would not catch.
do $$
declare
    escalated text;
begin
    select string_agg(attribute, ', ') into escalated
    from (
        select 'SUPERUSER'   as attribute from pg_roles where rolname = 'akmarkets_app' and rolsuper
        union all
        select 'BYPASSRLS'      from pg_roles where rolname = 'akmarkets_app' and rolbypassrls
        union all
        select 'CREATEROLE'     from pg_roles where rolname = 'akmarkets_app' and rolcreaterole
        union all
        select 'CREATEDB'       from pg_roles where rolname = 'akmarkets_app' and rolcreatedb
        union all
        select 'REPLICATION'    from pg_roles where rolname = 'akmarkets_app' and rolreplication
    ) as found;

    if escalated is not null then
        raise exception 'akmarkets_app holds privileges it must never have: %', escalated;
    end if;
end
$$;

grant usage on schema public to akmarkets_app;
-- The runtime role must not be able to create objects, including a table
-- that would shadow one of ours.
revoke create on schema public from akmarkets_app;

-- Table privileges are enumerated one by one. `grant ... on all tables in
-- schema public` would hand this application every other table in a shared
-- database, which is exactly the cross-product coupling the mi_ namespace
-- exists to prevent.
grant select, insert, update, delete on mi_users             to akmarkets_app;
grant select, insert, update, delete on mi_portfolios        to akmarkets_app;
grant select, insert, update, delete on mi_portfolio_members to akmarkets_app;
grant select, insert, update, delete on mi_instruments       to akmarkets_app;
grant select, insert, update, delete on mi_transactions      to akmarkets_app;
-- Read-only: the application verifies the schema version at startup and
-- must never be able to claim a migration ran.
grant select on mi_schema_migrations to akmarkets_app;

-- RLS is already enabled on these tables by 0001. With it on and no policy,
-- a non-owner role is denied everything, so the role needs an explicit
-- admission. Per-user authorisation is enforced in the application service
-- layer, which knows the request's user; the database's job here is to stop
-- any *other* role reaching this data at all.
create policy akmarkets_app_rw on mi_users             for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_portfolios        for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_portfolio_members for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_instruments       for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_transactions      for all to akmarkets_app using (true) with check (true);

-- Belt and braces on the PostgREST surface. 0001 revoked these already;
-- repeating it here means a future migration that adds a table and forgets
-- the revoke is the only way to reopen the hole, rather than an ALTER
-- DEFAULT PRIVILEGES change nobody notices.
revoke all on mi_users             from anon, authenticated;
revoke all on mi_portfolios        from anon, authenticated;
revoke all on mi_portfolio_members from anon, authenticated;
revoke all on mi_instruments       from anon, authenticated;
revoke all on mi_transactions      from anon, authenticated;
revoke all on mi_schema_migrations from anon, authenticated;

--@postgres-only-end
