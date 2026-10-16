# Architecture

Why the system is shaped this way, and what each decision was chosen over.

---

## 1. Four layers, one direction

```
Data platform      providers/     market data, filings, fundamentals, news, macro
Intelligence       domain/ ai/    calculation, signals, events, scoring, synthesis
Experience         web/ api/ cli/ browser now, iOS later
Execution          — out of scope, and unreachable from here
```

The intelligence platform must be fully operational without an execution
platform, so no module here may import one, and none does.

Inside the codebase the dependency direction is enforced:

```
providers ──┐
            ├──► services ──► web / api / cli
db ─────────┘        │
                     ▼
                  domain
```

`domain/` is at the bottom and imports nothing above it — not SQLAlchemy,
not FastAPI, not httpx. That is what allows a portfolio figure to be tested
against a hand-computed reference with no database and no network.

`tests/test_layering.py` parses every module's imports and fails the build on
a violation. A layering rule that is only written down is a layering rule
that quietly stops being true.

**Rejected: a flat layout.** Putting dozens of modules directly under one
package and every route inline in a single `create_app()` is a reasonable
shape for a small app. This product's surface is much larger, and the same
shape here would produce a `web.py` nobody can review.

---

## 2. One source of business truth

```
CLI  ─┐
Web  ─┼─► services/ ─► domain/
API  ─┘
```

There is no web-only calculation and no CLI-only calculation. A number on a
page and the same number from a command come from one function, so they
cannot drift apart. A future iOS client enters at `api/` and reaches the
same services.

Templates contain no business logic. A route reads request parameters, calls
one service, and renders.

---

## 3. Exact decimals

Money and share quantities are `Decimal` everywhere. `float` fails this
application in two specific ways:

1. A portfolio value is a sum of hundreds of price × quantity products, and
   the accumulated representation error lands in a figure compared against a
   broker statement to the cent.
2. The tax-lot engine decides whether a lot is *closed* by comparing a
   remaining quantity to zero. A residue of `1e-17` shares makes a closed
   position permanently open.

Postgres stores these as `NUMERIC`. SQLite has no decimal type, and
SQLAlchemy's `Numeric` there round-trips through a float. So
`akmarkets/db/types.py` stores the string form on SQLite and parses it back:
exact on both backends, identical in Python.

**The cost, stated so nobody trips on it:** on SQLite those columns sort and
aggregate lexicographically, so `ORDER BY amount` and `SUM(amount)` in SQL
are wrong. Nothing does either — the layering rule already puts all money
arithmetic in `domain/`, which loads rows and sums `Decimal` in Python. A
future aggregate that genuinely must run in SQL needs a Postgres-only code
path, not a change to the type.

---

## 4. Schema is data, not code

The application verifies its schema and refuses to serve on a mismatch. It
never creates, alters, drops or repairs.

- Migrations are files: `migrations/NNNN_lower_snake.sql`.
- `mi_schema_migrations` records version, name, SHA-256 checksum, applied time.
- Each file applies in **its own transaction**, so a failure at file three
  leaves one and two durably applied and the ledger honest about where the
  database actually is.
- A file edited *after* it was applied is detected by checksum and reported
  as its own failure, distinct from "pending" — re-running cannot reconcile
  it, so it needs a human.
- Startup failures are held on application state rather than raised. A
  process that dies in `lifespan` takes down the health page, making the one
  surface that could explain the outage its first casualty.

Portability: files run on Postgres and SQLite alike. Statements with no
SQLite equivalent — roles, `GRANT`, RLS — are wrapped:

```sql
--@postgres-only-begin
alter table mi_users enable row level security;
--@postgres-only-end
```

There is deliberately no SQLite-only block. Needing one would mean the two
backends had diverged in *behaviour* rather than syntax, which is the
situation this design exists to prevent.

**Rejected: `Base.metadata.create_all()` on startup.** It silently does nothing when a table exists with drifted
columns, so it appears to work right up until a query fails in production,
and it can only ever add. It also requires the runtime role to hold DDL
privileges, which would defeat the least-privilege model below.

---

## 5. Database isolation

The design assumes the Postgres instance may be shared with other
applications, partitioned by table prefix. This one owns `mi_`.

- `akmarkets_app`: login, **no** superuser, `BYPASSRLS`, `CREATEROLE` or DDL.
  Granted on `mi_*` only.
- RLS enabled on every `mi_*` table, with one policy admitting only that role.
- `anon` and `authenticated` revoked, so nothing is reachable through
  PostgREST. The application connects over Postgres directly.

Sharing a physical database is not sharing application data. If another
product ever needs something from here, it gets an explicit interface, not a
cross-schema `SELECT`.

---

## 6. Identity now and later

An authenticating reverse proxy (for example Cloudflare Access) is the
phase-one perimeter, and it is a perimeter only.
Internally the application has real users from the first migration: stable
UUIDs in `mi_users`, ownership and roles in `mi_portfolio_members`
(owner / admin / viewer), portfolio ownership separate from identity.

An auth adapter maps the proxy-provided identity to a user row. Adding
native token authentication for iOS replaces that adapter and nothing else.
Building this as a single-user site now would mean rebuilding it later,
because a native client cannot depend on browser-style Access sessions.

---

## 7. Two surfaces

| | Static snapshot | Interactive app |
|---|---|---|
| Built by | `ak-markets export`, hosted on any static host | FastAPI |
| Content | last-known-valid read views | everything, plus writes |
| Available when | always | when the service is up |

The static export is a read-optimised cache. **The database and the backend
services are the source of truth** — never the generated HTML, never browser
storage, never a future iOS local cache. The snapshot always states its own
freshness rather than implying currency.

Export safety, carried over from a proven implementation: build into
`<out>.partial`, validate that required files exist, then atomically rename
over the target. Never publish a half-built snapshot. Detail pages are
written as `<name>/index.html`, because a ticker like `BRK.B` otherwise has
its `.B` read as a file extension.

---

## 8. Data, honestly

```
observation ──► signal ──► event ──► intelligence conclusion
 "volume =      "abnormal   "production   "operational outlook
  2.1× 20d avg"  volume"     update"       improved"
```

Every level keeps a link to the one below it, so a conclusion can be
traced back to the numbers that produced it.

No provider is load-bearing. Each category declares a Protocol; adapters
implement it; a fallback chain tries them in configured order. Every value
carries `source`, `effective_at`, `retrieved_at` and a status. Providers
disagreeing beyond a configured tolerance produce a recorded data-quality
finding, not a silent pick of whichever answered first.

Freshness is per dataset, never one global "last updated" — quotes, news,
fundamentals and technicals age at completely different rates and a single
timestamp would lie about three of them.

Price-cache freshness asks "do we hold the last completed session's close?"
against a real exchange calendar, not "is this file less than N hours old".
On a daily schedule every file is always older than any fixed window, so a
time-based test either always refetches or never does.

---

## 9. AI is a tier, not a centre

```
Tier 0  deterministic code       ← almost everything
Tier 1  local models             sentiment, embeddings, dedup, clustering
Tier 2  economical LLM           short summaries, easy classification
Tier 3  strong reasoning model   earnings interpretation, thesis change, Ask
```

Expensive tiers run because something meaningfully changed, not on a timer:

```
new data → deterministic diff → relevance → novelty → materiality
         → cheap classification → strong model only if justified
```

The "What Changed" engine is therefore both a product feature and the
primary cost control.

Privacy: a context builder selects the minimum structured facts a question
needs and bands anything sensitive — `portfolio exposure: HIGH`, not
`owns $52,431.17 of XMPL`. The model never gets database access.

Everything AI is off by default, behind a flag, under a hard daily cost
ceiling, with per-feature spend recorded. The product is required to be
fully usable with the whole tier stack disabled.

---

## 10. Failure is a state, not an exception

A pipeline stage that fails is recorded, and the rest of the pipeline
continues where it is safe to. One news provider timing out must not fail a
portfolio revaluation that needs nothing from it. The run finishes with a
`degraded` badge listing which stages fell over — distinct from a failed run,
because calling a mostly-successful run "failed" trains people to ignore the
status.

Anything slow narrates itself through the activity log, streamed over SSE.
The stream is resumable by event id, so reconnection is correct with no
server-side subscription state.
