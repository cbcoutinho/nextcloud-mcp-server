# ADR-026: Pluggable database backend (DATABASE_URL)

## Status

Accepted — 2026-05-16

## Context

`RefreshTokenStorage` (in `nextcloud_mcp_server/auth/storage.py`) holds all
of the MCP server's persistent state: refresh tokens, OAuth client
credentials, OAuth sessions, browser sessions, app passwords, login-flow
sessions, audit logs, and webhook registrations. Until this ADR it was
backed by a single SQLite file, with the path configured by
`TOKEN_STORAGE_DB`.

This works well for single-user deployments but blocks horizontal scaling
in Kubernetes:

- Every pod needs its own PVC (ReadWriteOnce) or a ReadWriteMany volume.
- Tokens stored on pod A are invisible to pod B, so a Service can only
  route traffic to one pod at a time.
- Restart / re-deploy cycles either drop the volume (token loss) or
  require coordinated PVC handling.
- Backup, encryption-at-rest, and multi-region replication become
  per-pod concerns rather than centrally managed DB concerns.

We needed a way for pods to be stateless and share a centralized store
without giving up the zero-config SQLite path that single-user installs and
local development rely on.

## Decision

Introduce a `DATABASE_URL` setting that accepts any SQLAlchemy async URL,
with `sqlite+aiosqlite:///...` remaining the default. The runtime keeps a
single linear migration history and a single `RefreshTokenStorage` class —
the backend is selected purely by the URL.

### Resolution order

`get_database_url()` (in `nextcloud_mcp_server/config.py`) returns:

1. `DATABASE_URL` if set — wins over everything.
2. Otherwise `sqlite+aiosqlite:///{get_token_db_path()}`, so the legacy
   `TOKEN_STORAGE_DB` env var and the process-local ephemeral tempfile
   fallback both keep working unchanged.

### Why SQLAlchemy Core + async engine, not an ABC with parallel drivers

Two alternatives were considered:

| Option | Why rejected |
|---|---|
| Define a `Storage` ABC with `SQLiteStorage` (aiosqlite) and `PostgresStorage` (asyncpg) implementations | Doubles the surface area — every schema change has to land in two backends, with two sets of migrations, two SQL dialects, two upsert idioms. Diverges over time. |
| Switch to a full SQLAlchemy ORM (declarative models) | Larger refactor; the existing explicit-SQL style is intentional and well-understood by reviewers. |
| **Keep `RefreshTokenStorage` and put SQLAlchemy Core under it** *(chosen)* | One method body per operation, one migration history (Alembic is already SQLAlchemy-based). The URL drives dialect, pool, and DDL. |

A thin compatibility shim (`_DBConn` / `_Cursor` / `_Row` in `storage.py`)
adapts the `async with aiosqlite.connect(...) as db: async with
db.execute(...) as cursor: ...` idiom to SQLAlchemy `AsyncEngine` /
`AsyncConnection`. Existing method bodies needed only their connection
context-manager swapped; `?` placeholders are rewritten to named binds on
the fly. The seven `INSERT OR REPLACE` statements were rewritten as
portable `INSERT ... ON CONFLICT (...) DO UPDATE` (SQLite ≥ 3.24, Postgres
≥ 9.5; we already require SQLite ≥ 3.35 elsewhere).

### Distribution: psycopg is an optional extra, bundled in Docker

> **Amendment (Model A):** the backend now uses **psycopg3** for every
> Postgres connection (app engine *and* the procrastinate queue); `asyncpg`
> has been dropped. `DATABASE_URL` is consumed **verbatim** and TLS is
> configured in the URL (`?sslmode=...`) — see the TLS section below.

`psycopg[binary]` carries a compiled component — too heavy a default for the
`pip install nextcloud-mcp-server` audience, the majority of whom run the
SQLite path. It is shipped as a PyPI optional dependency::

    pip install 'nextcloud-mcp-server[postgres]'

The published Docker image runs `uv sync --extra postgres` so the
container always has the driver, matching the HA-deployment audience
that exercises the Postgres backend. When `DATABASE_URL=postgresql+psycopg://...`
is set on a venv without the extra installed, `RefreshTokenStorage`
raises a clear actionable error before the engine is built — operators
see "install with `[postgres]` extra" rather than a generic
`ModuleNotFoundError: No module named 'psycopg'`.

### Alembic env.py runs the async engine inside a worker thread

`nextcloud_mcp_server/alembic/env.py` uses
`async_engine_from_config(...)` + `anyio.run(run_async_migrations)`, and
the runtime invokes it from `RefreshTokenStorage.initialize()` via
`anyio.to_thread.run_sync(upgrade_database, ...)`. This is intentional:

- Alembic wants a synchronous entry point (`upgrade_database()`), but
  `async_engine_from_config` returns an async engine.
- Running `anyio.run()` directly inside an already-running event loop
  would deadlock; we have to be on a different thread.
- `to_thread.run_sync` puts the call on a worker thread, which has no
  running event loop — `anyio.run()` is then free to spin up its own.

The pattern is non-obvious; this note exists so a future maintainer
doesn't try to "simplify" it back into the main loop.

### Concurrency model and pool sizing

The reviewer's natural reaction to seeing `DATABASE_POOL_SIZE=10` (the
round-2 default) was: *isn't 1 connection enough for an MCP server?*
This subsection records why a small pool is right, why 1 is not the
target default, and what the workload actually looks like.

**psycopg connection semantics.** Each psycopg connection is
**single-flight** — only one query can be in flight at a time on a
given connection. SQLAlchemy serializes additional requests in the
pool queue. So the question is never "how many requests does the MCP
server handle" but "how many concurrent storage operations are in
flight at the peak".

**MCP storage workload shape.** Each MCP tool call typically performs
1–3 storage operations: a token lookup (`get_refresh_token` or
`get_app_password`), maybe an audit-log write, occasionally a session
update. Lookups are sub-millisecond point queries; writes are short.
The hot path is read-mostly. No long-running transactions, no batch
loads.

**Why not 1?** A single-user (homelab) deployment genuinely works on
`pool_size=1, max_overflow=2`. But the default ships for multi-user
OAuth deployments where ≥2 concurrent client requests are normal; on
`pool_size=1` those serialize on a single connection and you measure
a latency cliff. The defaults `pool_size=2, max_overflow=5` (max 7
per pod) cover typical multi-user MCP burst with two-replica
headroom. With 3 k8s replicas the total is 21 connections — well
under managed-Postgres `max_connections=100` (RDS, CNPG default).

**How to tune.** `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` env
vars adjust the per-pod pool live (server restart). The startup
``Postgres engine ready: pool_size=N max_overflow=M (per-pod max K
connections)`` log line surfaces the active sizing so operators can
see the per-replica footprint at a glance. For a fleet of N replicas,
estimate worst-case Postgres connection count as
`N × (pool_size + max_overflow)` and stay comfortably below the
server's `max_connections`.

### Concurrent migrations across pods

When `replicas: N` rolling-update restarts, multiple pods race
`RefreshTokenStorage.initialize()` simultaneously. Alembic's
version-table UPDATE isn't write-locked across connections by
default; without coordination, two pods can both observe
"no `alembic_version` table" and both try to apply migrations from
scratch — the second one crashes with `relation … already exists`.

We serialize this with a session-level Postgres advisory lock
(`SELECT pg_advisory_lock(:lock_id)`) acquired in `_migration_lock()`
and held across BOTH the schema inspection and the migration call.
The lock ID is a stable 64-bit integer derived from
`sha256("nextcloud-mcp-server:migrations")[:8]` so we can't collide
with other apps that happen to share the same Postgres instance.
The second pod blocks at the advisory-lock call until the first pod
finishes; it then re-inspects the schema, sees the now-populated
`alembic_version` table, and takes the no-op upgrade fast path.

SQLite needs no equivalent: file-level locking serializes writes
natively, so the second process waits on the file lock and then
sees the migrated schema. Covered by
`tests/integration/test_storage_postgres.py::test_concurrent_initialize_serialized_by_advisory_lock`.

### TLS for the Postgres backend

> **Amendment (Model A):** TLS is configured **entirely in `DATABASE_URL`**
> and read by libpq/psycopg. The earlier `DATABASE_VERIFY_SSL` /
> `DATABASE_CA_BUNDLE` env vars and the `get_database_ssl()` helper have been
> removed. The text below records the superseded design.

Because the server now uses psycopg3 (libpq) for both the app engine and the
procrastinate connector and passes `DATABASE_URL` through verbatim, TLS is
expressed with standard libpq query parameters on the URL:

- `?sslmode=require` — encrypt, do not verify the certificate (the common
  cluster-internal posture; matches the historical default below).
- `?sslmode=verify-full&sslrootcert=/path/to/ca.pem` — verify against a
  private CA (homelab / managed Postgres with a known CA).
- Omit `sslmode` entirely → libpq's default (`prefer`): TLS if offered, no
  verification.

There is no separate env-var TLS mechanism and the server neither parses nor
validates the URL — a bad value fails fast at the driver. This keeps a single
source of truth (the DSN) shared by the app engine and the queue, and was the
fix for the `Dropping DATABASE_URL query parameters ... sslmode` warning that
the old decomposition emitted.

<details><summary>Superseded env-var TLS design</summary>

Two settings mirrored the existing `NEXTCLOUD_VERIFY_SSL` /
`NEXTCLOUD_CA_BUNDLE` pattern: `DATABASE_VERIFY_SSL` and `DATABASE_CA_BUNDLE`,
resolved by `get_database_ssl()` and passed to asyncpg via
`connect_args={"ssl": ...}`. The default was deliberately less strict than the
Nextcloud HTTPS default (`None`, i.e. asyncpg's `prefer`) because
cluster-internal Postgres frequently ran without TLS. This mechanism was
asyncpg-shaped and inert for the psycopg engine, so it was removed in favour of
the in-URL `sslmode` above.

</details>

### Encryption stays in Python (Fernet), not the DB

The DB only ever sees ciphertext for sensitive columns
(`encrypted_token`, `encrypted_client_secret`, `encrypted_password`,
`encrypted_poll_token`). The Fernet key remains a `TOKEN_ENCRYPTION_KEY`
env var, applied in Python before INSERT and after SELECT. This means:

- Switching backends does not invalidate or re-key existing data.
- Postgres-level features like `pgcrypto` are not required.
- Operators rotating the encryption key still go through the existing
  Python path.

### DDL portability

All Alembic migrations were rewritten from raw `op.execute("CREATE TABLE
...")` strings to `op.create_table()` / `op.create_index()` calls with
SQLAlchemy types. Notable choices:

- All `*_at` / expiration / timestamp columns use `sa.BigInteger` —
  Postgres `INTEGER` is 32-bit and unix epochs are already past that range.
  SQLite treats `BIGINT` and `INTEGER` identically (dynamic typing) so
  this is backwards compatible.
- `BLOB` → `sa.LargeBinary` (becomes `BYTEA` on Postgres).
- `BOOLEAN DEFAULT FALSE` → `sa.Boolean, server_default=sa.false()`.
- Existing SQLite deployments are at revision `006` and skip the
  rewritten migrations entirely — content rewrites are safe.

### No data migration, no shipped Postgres

Two scope decisions worth recording:

1. **Clean cutover, no SQLite → Postgres data migration tool.** Tokens
   are reissued on the next login; webhooks re-register on the next sync
   tick. Acceptable because the ephemeral-default already implies this,
   and the data being preserved (audit logs, OAuth sessions) is either
   short-lived or reconstructible.
2. **Bring-your-own database.** The MCP server consumes a
   `DATABASE_URL`; it does not provision Postgres itself. Operators use
   CNPG, RDS, the project's existing Helm chart with a sub-chart, etc.
   The `postgres-test` service in `docker-compose.yml` exists only for
   integration tests and manual HA smoke testing — it is gated on the
   `postgres` profile and is not the recommended production pattern.

### CLI changes

The `nextcloud-mcp-server db {upgrade,downgrade,current,history}` commands
gain a `--database-url / -u` flag (env `DATABASE_URL`) alongside the
existing `--database-path / -d` (env `TOKEN_STORAGE_DB`). `-u` wins over
`-d`; both fall back to `get_database_url()`.

## Consequences

### Positive

- MCP server pods become stateless. A Kubernetes Deployment can run with
  `replicas: 3` behind a Service, with all pods pointed at the same
  Postgres URL — tokens written by pod A are immediately visible to pod B.
- Centralized DB operations (backup, restore, replication, encryption at
  rest, monitoring) are handled by the operator's existing Postgres
  infrastructure rather than duplicated per-pod.
- No regression for single-user / local-development / docker-compose
  installs — the SQLite tempfile path is unchanged and remains the default
  when `DATABASE_URL` is unset.
- Test coverage doubles automatically: every test that uses the
  `temp_storage` fixture now runs against both SQLite and Postgres when
  `TEST_DATABASE_URL` is exported.

### Negative

- One more thing operators have to think about for HA deployments
  (Postgres connection string, credentials secret, network policies).
- Adds SQLAlchemy + psycopg to the runtime dependency set. SQLAlchemy was
  already transitively present via Alembic; psycopg (also required by the
  procrastinate queue) is the single Postgres driver.
- The compatibility shim in `storage.py` is a small piece of bespoke code
  that future contributors need to understand. The alternative — rewriting
  every method body to SQLAlchemy idioms — was rejected as too risky for
  this PR but might be revisited.

### Neutral

- The Alembic migration history was content-rewritten but its revision
  graph is unchanged (still `001 → 006`), so existing SQLite deployments
  do not re-run anything.
- `TOKEN_STORAGE_DB` still works exactly as before; deployments that
  already set it require no changes.

## Related

- [ADR-022 Login Flow v2](ADR-022-deployment-mode-consolidation.md) —
  defines the per-user app password storage that this ADR centralizes.
- [ADR-002 Vector sync authentication](ADR-002-vector-sync-authentication.md)
  — explains the offline-access tokens that benefit most from HA storage.

## Verification

1. `uv run pytest tests/unit/` — SQLite path unchanged (1012 tests).
2. `docker compose --profile postgres up -d postgres-test` then
   `TEST_DATABASE_URL=postgresql+psycopg://mcp:mcp@localhost:5433/mcp uv run pytest tests/unit/test_app_password_storage.py tests/unit/test_webhook_storage.py`
   — every test runs once per backend.
3. Manual end-to-end smoke against `mcp-login-flow` with a Postgres URL
   (commands in `/home/chris/.claude/plans/spicy-enchanting-flurry.md` →
   Verification).
4. k8s HA validation (after merge in `homelab-argocd`): `replicas: 3`,
   confirm session continuity through the Service.
