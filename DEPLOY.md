# Deployment

Everything below is a template. Replace each placeholder (`YOUR_...`, `example.com`) with your own values. Nothing here is tied to a specific account, domain or provider, and no credentials belong in the repository: pass them as environment variables or repository secrets.

The platform stores portfolio data. Treat any hosted copy as private by default and put it behind authentication before the first visit.

```
scheduled job ──► Postgres (optional) ◄── FastAPI app (behind an authenticating proxy)
      │
      └──► static export (optional) ──► any static host, behind access control
```

---

## 1. Local, with no cloud credentials

```bash
uv sync
uv run ak-markets db migrate
uv run ak-markets serve      # http://127.0.0.1:8770
```

State goes to `var/akmarkets.sqlite3`. `var/` holds runtime state (the SQLite file, price and fundamentals caches, reports). It is ignored by git, created on demand and safe to delete: everything in it is reproducible from the data providers, apart from the SQLite database itself, which holds your portfolios.

Set `AKM_EDGAR_CONTACT` to an address you control before fetching fundamentals. SEC EDGAR requires a contactable User-Agent, and the default is the placeholder `you@example.com`:

```bash
export AKM_EDGAR_CONTACT='you@yourdomain.example'
```

---

## 2. Postgres

Any Postgres 14+ works. Hosted providers such as Supabase are fine. Use a **session-mode** connection (port 5432 on most poolers) with the `postgresql+psycopg://` scheme:

```
postgresql+psycopg://YOUR_APP_ROLE:YOUR_PASSWORD@YOUR_HOST:5432/YOUR_DB
```

Notes:

- If you run jobs from CI, make sure the host resolves over IPv4. Many CI runners are IPv4-only, so a database that publishes only an IPv6 address fails DNS resolution before authentication. Connection poolers usually publish IPv4.
- Avoid transaction-mode poolers unless you need them. The application disables server-side prepared statements either way.
- A password containing `@`, `/` or `%` is handled: `normalize_database_url` splits credentials on the last `@` and percent-encodes. Paste it raw.

### Roles and privileges

`migrations/` contains the role, `GRANT`, `REVOKE` and row-level-security statements, so privileges are versioned with the code rather than living in somebody's SQL editor history.

- An owner credential applies migrations.
- `akmarkets_app` is the runtime role: login, no superuser, no `BYPASSRLS`, no `CREATEROLE`, no DDL, granted on `mi_*` tables only.
- RLS is enabled on every `mi_*` table, and `anon` and `authenticated` are revoked so nothing is exposed through an HTTP data API.

The role is created without a password. Set one yourself in your database console and keep it out of any transcript:

```sql
alter role akmarkets_app with login password 'choose-a-long-random-one';
```

Then:

```bash
# Migrations need an owner credential; the app's own role cannot run DDL.
AKM_MIGRATION_DATABASE_URL='postgresql+psycopg://OWNER:PASSWORD@YOUR_HOST:5432/YOUR_DB' \
  uv run ak-markets db migrate

# The app then runs as the scoped role.
AKM_DATABASE_URL='postgresql+psycopg://akmarkets_app:PASSWORD@YOUR_HOST:5432/YOUR_DB' \
  uv run ak-markets status
```

Install the driver with `uv sync --extra postgres`.

---

## 3. The perimeter (read this before exposing the app)

The application does not implement its own login. It identifies users by an email header set by an authenticating reverse proxy in front of it (the default header is `Cf-Access-Authenticated-User-Email`, as set by Cloudflare Access). **The header is trusted only because the perimeter is.** Anyone who can reach the origin directly can forge it.

Therefore:

- Run with `AKM_ENV=production`. In that mode a request with no identity header is refused with a 403. Without it, every request is served as the development user `local@localhost`, silently.
- Never expose the origin port directly. Bind to loopback and let the proxy or tunnel connect to it, or restrict the network path so only the proxy can reach it.
- Do not set `PORT` unless you run in a container that needs it. When `PORT` is set the server binds `0.0.0.0` instead of `127.0.0.1`.
- Put an access policy in front of **every** hostname that reaches the app or a published snapshot, including any default hostname your hosting provider assigns. Protecting only the custom domain can leave the same content readable at the provider's own URL.
- Verify with a request that carries no identity header and expect a redirect to the login page or a 403, and repeat it several times because policy changes can take a while to propagate to every edge.

Robots directives and security headers are set on the static export (`X-Robots-Tag: noindex, nofollow`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`), but they are not authentication.

---

## 4. Interactive app

```bash
AKM_ENV=production \
AKM_DATABASE_URL='postgresql+psycopg://...' \
AKM_SESSION_SECRET='a-long-random-string' \
  uv run ak-markets serve
```

The server listens on `127.0.0.1:8770` by default. Put it behind your authenticating proxy or tunnel. If you keep it running as a service (systemd, launchd, a container), restart it after changing Python code: Jinja templates hot-reload but Python modules do not, so a long-running process can serve new templates through old code.

---

## 5. Static export

```bash
uv run ak-markets export --out dist --owner you@example.com
```

The export is a read-only snapshot of one owner's portfolios. Upload `dist/` to any static host and put access control in front of it (section 3). The exporter builds into `dist.partial`, checks that required files exist, then renames atomically over `dist`, so a failed build never replaces a good snapshot with a half-rendered one.

---

## 6. Scheduled jobs (GitHub Actions)

`.github/workflows/checks.yml` runs ruff, mypy and the test suite on every push. It needs no secrets.

`deploy/examples/` contains two scheduled workflows (an end-of-day refresh and a weekly full refresh). Copy them to `.github/workflows/` in your own repository and add a repository secret:

| Secret | Value |
|---|---|
| `AKM_DATABASE_URL` | `postgresql+psycopg://...` URL for the runtime role |

Optional provider keys can be added the same way (see below).

Both workflows run `ak-markets db check` first, so a connection problem is reported as one before anything is blamed on the application. They use a rolling cache of `var/prices` and `var/fundamentals`; daily bars never change once a session has settled, so refetching five years of history every night would be waste.

Private-repository Actions minutes are metered by your plan, so budget the schedule accordingly.

---

## 7. Environment variables

| Variable | Secret | Purpose |
|---|---|---|
| `AKM_DATABASE_URL` | yes | Postgres URL for the runtime role (default: local SQLite) |
| `AKM_MIGRATION_DATABASE_URL` | yes | Owner credential, migrations only |
| `AKM_SESSION_SECRET` | yes | Generated if unset |
| `AKM_EDGAR_CONTACT` | no | Contact address for the SEC EDGAR User-Agent (default: `you@example.com`) |
| `AKM_ENV` | no | `development` (default) or `production` |
| `PORT` | no | Set by container hosts; overrides the configured port and binds all interfaces |

Optional provider keys resolve from the environment first, then the OS keychain (`ak-markets secrets set ...`), so one code path serves CI and a laptop.

---

## 8. Backups

Managed Postgres backups are not the same as application-level recovery. What still needs to be true:

- The `mi_*` tables can be restored independently of neighbouring applications.
- Migrations roll forward only. A rollback is a new migration, written and reviewed like any other.
- Everything under `var/` except the SQLite database is a cache and is reproducible from the providers.
- A restore has to be tested, not assumed.
