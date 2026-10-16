-- Row-level security on the migration ledger itself.
--
-- 0001 and 0002 enabled RLS on every application table but not on
-- `mi_schema_migrations`, because that table is created by the migration
-- runner rather than by a migration file. It is already revoked from `anon`
-- and `authenticated`, so it was never reachable through PostgREST — but
-- "no grant" and "no RLS" is a weaker position than both, and Supabase's
-- own advisors correctly flag a public table with RLS off.
--
-- Fixed forward in a new file rather than by editing 0002. Those two are
-- already applied and checksummed; editing one would be detected as an
-- altered migration and refuse to start, which is exactly the protection
-- working as designed.

--@postgres-only-begin
alter table mi_schema_migrations enable row level security;

-- Read-only for the application: startup verifies the schema version, and
-- must never be able to claim a migration ran. The migration runner uses an
-- owner credential, which is not subject to this policy.
create policy akmarkets_app_read on mi_schema_migrations
    for select to akmarkets_app using (true);

revoke all on mi_schema_migrations from anon, authenticated;
--@postgres-only-end
