# Configuration

The Nextcloud MCP server requires configuration to connect to your Nextcloud instance. Configuration is provided through environment variables, typically stored in a `.env` file.

> **Note:** Configuration was significantly simplified in v0.58.0. If you're upgrading from v0.57.x, see the [Configuration Migration Guide](configuration-migration-v2.md).

## Quick Start

We provide mode-specific configuration templates for quick setup:

```bash
# Choose a template based on your deployment mode:
cp env.sample.single-user .env         # Simplest - one user, local dev
cp env.sample .env                     # Full reference with all options

# For multi-user Login Flow v2 (recommended), see the dedicated guide:
# docs/login-flow-v2.md#setup

# Edit .env with your Nextcloud details
```

> **Note:** `env.sample.oauth-multi-user` is a Login Flow v2 quick-start template for multi-user setups. See [Login Flow v2](login-flow-v2.md).

Then choose your deployment mode:

- [Single-User BasicAuth](#single-user-basicauth-mode) - Simplest for personal instances
- [Multi-User BasicAuth](#multi-user-basicauth-mode) - Internal deployments with credential pass-through
- [Login Flow v2](#login-flow-v2-mode) - Recommended for hosted / OAuth-based MCP clients
- [Deployment Mode Selection](#deployment-mode-selection) - Explicit mode declaration

---

## Deployment Mode Selection

The server supports three deployment modes. See [Authentication](authentication.md) for the full comparison and [Login Flow v2](login-flow-v2.md) for the recommended multi-user setup.

| Mode | When to use |
|------|-------------|
| `single_user_basic` | Personal use, dev — credentials in env vars |
| `multi_user_basic` | Internal deployments — clients send credentials via `Authorization: Basic` header |
| `login_flow` | Hosted / OAuth-based MCP clients (claude.ai, Astrolabe Cloud) — recommended for multi-user |

You can declare the mode explicitly:

```dotenv
MCP_DEPLOYMENT_MODE=login_flow
```

If `MCP_DEPLOYMENT_MODE` is not set, the server auto-detects from the other env vars below.

---

## Single-User BasicAuth Mode

The simplest mode. Use for personal instances, local development, and testing.

```dotenv
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
NEXTCLOUD_USERNAME=your_nextcloud_username
NEXTCLOUD_PASSWORD=your_app_password
```

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXTCLOUD_HOST` | ✅ Yes | Full URL of your Nextcloud instance |
| `NEXTCLOUD_USERNAME` | ✅ Yes | Your Nextcloud username |
| `NEXTCLOUD_PASSWORD` | ✅ Yes | Use a dedicated [Nextcloud app password](https://docs.nextcloud.com/server/latest/user_manual/en/session_management.html#managing-devices), not your login password |

---

## Multi-User BasicAuth Mode

Each MCP client sends its own Nextcloud credentials in an `Authorization: Basic` header. The server passes them through per-request and never persists them.

```dotenv
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
MCP_DEPLOYMENT_MODE=multi_user_basic

# Optional: enable per-user app-password storage for background sync
TOKEN_ENCRYPTION_KEY=<fernet-key>
TOKEN_STORAGE_DB=/app/data/tokens.db
```

`NEXTCLOUD_USERNAME` and `NEXTCLOUD_PASSWORD` must NOT be set in this mode.

---

## Login Flow v2 Mode

The recommended multi-user mode. MCP clients authenticate to the MCP server via OAuth; the server holds per-user Nextcloud app passwords (encrypted) obtained via Login Flow v2.

```dotenv
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
MCP_DEPLOYMENT_MODE=login_flow

# App-password storage (required)
TOKEN_ENCRYPTION_KEY=<fernet-key>
TOKEN_STORAGE_DB=/app/data/tokens.db

# Static OIDC client for the MCP server's own IdP registration.
# Strongly recommended — with Nextcloud's built-in oidc app the DCR
# fallback expires after ~1h (see the warning below). Create the client
# under Administration settings → OpenID Connect provider.
NEXTCLOUD_OIDC_CLIENT_ID=<client-id-from-nextcloud>
NEXTCLOUD_OIDC_CLIENT_SECRET=<client-secret-from-nextcloud>

# Public URLs for browser redirects
NEXTCLOUD_MCP_SERVER_URL=https://mcp.example.com
NEXTCLOUD_PUBLIC_ISSUER_URL=https://your.nextcloud.instance.com
```

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXTCLOUD_HOST` | ✅ Yes | Internal URL of your Nextcloud instance (server-to-server) |
| `MCP_DEPLOYMENT_MODE` | ✅ Yes | Set to `login_flow` to select this mode. The Login Flow v2 browser-app-password layer is derived from the mode automatically — no separate flag needed. |
| `TOKEN_ENCRYPTION_KEY` | ✅ Yes | Fernet key for app-password encryption — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `TOKEN_STORAGE_DB` | ✅ Yes | Path to SQLite DB for stored app passwords (use a persistent volume) |
| `NEXTCLOUD_MCP_SERVER_URL` | ✅ Yes | Public URL of the MCP server (used as the audience claim and for browser redirects) |
| `NEXTCLOUD_PUBLIC_ISSUER_URL` | ✅ Yes | Public URL used as the OAuth issuer for JWT validation **and** (by default) the browser-reachable Nextcloud URL for Login Flow v2 redirects. When Nextcloud is its own IdP these coincide. |
| `NEXTCLOUD_PUBLIC_URL` | Optional (required for external IdPs) | Browser-reachable public URL of **Nextcloud** for Login Flow v2 login pages and elicitation links. Only needed when the OAuth issuer is a *separate* IdP (e.g. Keycloak/Cognito): there `NEXTCLOUD_PUBLIC_ISSUER_URL` points at the IdP, so set this to Nextcloud's own URL or the Login Flow v2 login page is built on the IdP origin and 404s. Falls back to `NEXTCLOUD_PUBLIC_ISSUER_URL` then `NEXTCLOUD_HOST` when unset. |
| `NEXTCLOUD_OIDC_CLIENT_ID` | ✅ Strongly recommended | OIDC client ID for the MCP server's relying-party registration with the IdP (Nextcloud's built-in OIDC by default; Keycloak / Cognito / etc. via `OIDC_DISCOVERY_URL`). If unset and the IdP advertises a `registration_endpoint`, the server falls back to RFC 7591 Dynamic Client Registration (DCR) — **but with Nextcloud's built-in `oidc` app this fallback breaks after ~1 hour** (see warning below). Create a static client and set this instead. |
| `NEXTCLOUD_OIDC_CLIENT_SECRET` | ✅ Strongly recommended | OIDC client secret paired with `NEXTCLOUD_OIDC_CLIENT_ID`. |
| `OIDC_DISCOVERY_URL` | Optional | Override the IdP discovery URL. Defaults to `${NEXTCLOUD_HOST}/.well-known/openid-configuration` (Nextcloud's built-in OIDC). Set to a Keycloak realm or AWS Cognito user-pool discovery URL to use an external IdP. |
| `OIDC_DISCOVERY_MAX_ATTEMPTS` | Optional (default `10`) | Number of attempts for the OIDC discovery fetch performed at startup. Discovery is retried on transport errors (e.g. connect timeouts) and 5xx responses with capped exponential backoff + jitter, so a cold-start network race (e.g. Cilium egress programming) doesn't crashloop the server. `4xx` responses (real misconfiguration) fail immediately. Set to `1` to restore fail-fast-on-first-error behavior. |
| `OIDC_DISCOVERY_BACKOFF_BASE` | Optional (default `1.0`) | Base delay in seconds for the first discovery retry; subsequent retries grow exponentially (`base * 2**n`) with full jitter. |
| `OIDC_DISCOVERY_BACKOFF_MAX` | Optional (default `15.0`) | Per-retry cap in seconds for the discovery backoff. With the defaults, worst-case startup blocks on the order of ~90s of backoff (`1+2+4+8+15×5`) **plus** the per-attempt connect timeouts (~5s each on the LOGIN_FLOW path, 30s on the hybrid multi-user-basic path) before a persistently-down IdP finally exits — size your k8s `startupProbe`/`livenessProbe` accordingly. |

> **⚠️ Use a static OIDC client with Nextcloud's built-in `oidc` app.** If you
> don't set `NEXTCLOUD_OIDC_CLIENT_ID` / `NEXTCLOUD_OIDC_CLIENT_SECRET`, the MCP
> server registers its own relying-party client via DCR. Nextcloud's `oidc` app
> treats DCR clients as **ephemeral** and deletes them after `client_expire_time`
> (default **3600s = 1 hour**), pruning on every `/authorize`. Once it's gone,
> authorization and token refresh fail and users hit an **"Access forbidden"**
> page — permanently, because the server keeps reusing the deleted client.
> Register a permanent client in **Administration settings → OpenID Connect
> provider** and set the two env vars. See
> [Login Flow v2 → Troubleshooting](login-flow-v2.md#troubleshooting) and
> [issue #907](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/907).

See [Login Flow v2](login-flow-v2.md) for full setup, scope reference, and troubleshooting.

---

## Centralized Token Storage (DATABASE_URL, Optional)

By default the MCP server stores tokens / sessions / app passwords in a
local SQLite file (`TOKEN_STORAGE_DB`, falling back to a per-process
tempfile). For HA Kubernetes deployments where you need multiple
stateless pods to share state, point the server at a centralized
database via `DATABASE_URL`.

```env
# Centralized Postgres backend (HA k8s deployments)
DATABASE_URL=postgresql+psycopg://mcp:secret@postgres.svc.cluster.local:5432/mcp?sslmode=require&connect_timeout=10
TOKEN_ENCRYPTION_KEY=<fernet-key>
```

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Optional | SQLAlchemy async URL for any supported backend. When set, wins over `TOKEN_STORAGE_DB`. Primary supported targets: `postgresql+psycopg://...` (recommended for HA) and `sqlite+aiosqlite:///...` (development). **Passed through verbatim** — the server never rewrites it. |
| `TOKEN_STORAGE_DB` | Optional | Legacy SQLite-only path. Used when `DATABASE_URL` is unset. Falls back to a per-process ephemeral tempfile when both are unset. |
| `DATABASE_POOL_SIZE` | Deprecated, no-op | Was per-pod SQLAlchemy pool size for the Postgres backend. The engine now uses `NullPool` (one fresh psycopg connection per checkout) to avoid cross-event-loop crashes under anyio TaskGroups — see [ADR-026 § Connection pool](ADR-026-pluggable-database-backend.md) and [#799](https://github.com/cbcoutinho/nextcloud-mcp-server/pull/799). Still accepted for backward compatibility; setting it has no effect. |
| `DATABASE_MAX_OVERFLOW` | Deprecated, no-op | Was per-pod burst connection cap on top of `DATABASE_POOL_SIZE`. Now ignored (see above). |

**TLS is configured in the URL, not via env vars.** The server uses
psycopg3 (libpq) for both the app engine and the procrastinate queue and
hands `DATABASE_URL` through untouched, so add libpq parameters directly to
the URL: `?sslmode=require` (encrypt), `?sslmode=verify-full&sslrootcert=/path/ca.pem`
(verify against a private CA). Omitting `sslmode` leaves libpq's default
(`prefer`). There are no `DATABASE_VERIFY_SSL` / `DATABASE_CA_BUNDLE` settings.

**Set `connect_timeout` for production.** Because the server passes
`DATABASE_URL` through verbatim, it no longer injects a default connect
timeout. Add `?...&connect_timeout=10` (seconds) to a production `DATABASE_URL`
so worker/API startup fails fast against an unreachable Postgres instead of
hanging indefinitely — libpq reads it directly (as it does `sslmode`).

The psycopg engine is `NullPool`-only: each `engine.connect()` opens
and tears down a fresh psycopg connection in the caller's current
event loop. On LAN-local Postgres the per-connection overhead is a
single round-trip (~5 ms), so the throughput cost is negligible for
the MCP server's traffic shape (low concurrency, bursty per-user
requests).

Homelab example (self-signed Postgres with a private CA):

```env
DATABASE_URL=postgresql+psycopg://mcp:secret@pg.lan:5432/mcp?sslmode=verify-full&sslrootcert=/etc/ssl/certs/homelab-ca.pem
TOKEN_ENCRYPTION_KEY=<fernet-key>
```

Notes:

- **PyPI extra required.** The `psycopg` driver is an optional extra so
  the default `pip install nextcloud-mcp-server` stays lean. Install
  with `pip install 'nextcloud-mcp-server[postgres]'` when using a
  Postgres URL. The Docker image bundles it by default. When
  `DATABASE_URL=postgresql+psycopg://...` is set without the extra,
  the server fails fast with a clear actionable error.
- **Bring-your-own DB.** The MCP server doesn't provision the database;
  it just consumes the URL. Use CNPG, RDS, your existing Helm chart's
  Postgres sub-chart, etc.
- **Encryption stays in the app.** `TOKEN_ENCRYPTION_KEY` (Fernet) is
  applied in Python; the database only ever sees ciphertext for
  sensitive columns. You don't need `pgcrypto`.
- **Schema is managed automatically.** On startup the server runs
  Alembic migrations against the configured backend. Existing SQLite
  deployments are stamped at the current revision and skip re-execution.
- **No data migration tool.** Moving from SQLite to Postgres is a clean
  cutover — tokens are reissued on the next login, webhooks
  re-register on the next sync tick.
- **Testing a Postgres backend locally:** `docker compose --profile
  postgres up -d postgres-test` then export
  `DATABASE_URL=postgresql+psycopg://mcp:mcp@localhost:5433/mcp`.

See [ADR-026 Pluggable database backend](ADR-026-pluggable-database-backend.md)
for the architecture rationale.

---

## SSL/TLS Configuration (Optional)

If your Nextcloud instance uses a self-signed certificate or a private CA (common with reverse proxies like Traefik or Caddy), the MCP server will reject the connection by default. Use these settings to configure certificate verification.

### Custom CA Bundle (Recommended)

Point the server at your CA certificate file:

```dotenv
NEXTCLOUD_CA_BUNDLE=/etc/ssl/certs/my-ca.pem
```

With Docker, mount the certificate as a read-only volume:

```bash
docker run \
  -v /path/to/my-ca.pem:/etc/ssl/certs/my-ca.pem:ro \
  -e NEXTCLOUD_CA_BUNDLE=/etc/ssl/certs/my-ca.pem \
  -e NEXTCLOUD_HOST=https://nextcloud.local \
  --env-file .env \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

### Disable Verification (Development Only)

> [!WARNING]
> Disabling TLS verification is insecure. Only use this for local development or testing.

```dotenv
NEXTCLOUD_VERIFY_SSL=false
```

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEXTCLOUD_VERIFY_SSL` | ⚠️ Optional | `true` | Set to `false` to disable TLS certificate verification |
| `NEXTCLOUD_CA_BUNDLE` | ⚠️ Optional | - | Path to a PEM CA bundle file for custom certificate authorities |
| `NEXTCLOUD_HTTP_KEEPALIVE` | ⚠️ Optional | `true` | Reuse pooled keep-alive connections for the Nextcloud httpx client. Set to `false` to open a fresh connection per request. See the note below. |

> **`NEXTCLOUD_HTTP_KEEPALIVE`** — With the default (`true`) the httpx client pools
> and reuses connections. Setting it to `false` builds the transport with
> `max_keepalive_connections=0`, so every request opens (and closes) its own
> connection. This is recommended for deployments behind a flaky CDN/WAN path,
> where a truncated/interrupted response can poison a pooled connection and make
> later reads silently return empty bytes — see
> [#965](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/965). It
> mirrors the `DATABASE_POOL_SIZE`→`NullPool` precedent at the HTTP layer. The
> trade-off is a TLS handshake per request, which is negligible for a background
> indexer.

### Scope

These settings apply to **all** outbound connections to Nextcloud and its OIDC endpoints, including:

- Nextcloud API calls (Notes, Calendar, Contacts, WebDAV, etc.)
- OIDC discovery and token endpoints
- OAuth client registration (DCR)
- Health checks

They do **not** affect connections to internal services (Ollama, Qdrant, Unstructured) which have their own SSL configuration.

---

## Health & Readiness Probes

The server exposes two Kubernetes probe endpoints:

- `GET /health/live` — liveness. Returns `200` whenever the process is running. It does **not** check external dependencies, so it never restarts the Pod on an upstream blip.
- `GET /health/ready` — readiness. Gates **only** on local configuration (`NEXTCLOUD_HOST` set, auth mode configured). External-dependency reachability (Nextcloud `status.php`, Qdrant `/readyz`) is reported in the response body for observability but is **non-gating**.

> **Why non-gating (Deck #302):** the server typically runs as a single replica per tenant. If readiness failed whenever Nextcloud or Qdrant had a transient blip, the only Pod would be pulled from its Service, leaving the gateway with no upstream — turning a *degraded* dependency into a *total* outage and dropping every MCP client's streamable-HTTP session. Dependency health is instead refreshed by a background loop and cached, so the probe path performs no external I/O.

```dotenv
# Cadence (seconds) for the background dependency-health refresh loop (default: 15)
HEALTH_READY_REFRESH_INTERVAL=15
```

The probe reports each dependency under `checks` (`ok` / `embedded` / `pending` / `error: ...`); a non-`ok` dependency no longer flips the overall `status` to `not_ready`.

---

## Semantic Search Configuration (Optional)

**New in v0.58.0:** Simplified semantic search configuration with automatic dependency resolution.

The MCP server includes semantic search capabilities powered by vector embeddings. This feature requires a vector database (Qdrant) and an embedding service.

### Quick Start

**Single-User Mode:**
```dotenv
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password

# Enable semantic search
ENABLE_SEMANTIC_SEARCH=true

# Vector database
QDRANT_LOCATION=:memory:

# Embedding provider
OLLAMA_BASE_URL=http://ollama:11434
```

**Multi-User Login Flow v2 Mode:**
```dotenv
NEXTCLOUD_HOST=https://nextcloud.example.com
MCP_DEPLOYMENT_MODE=login_flow

# Enable semantic search
# In multi-user modes, this AUTOMATICALLY enables background operations!
ENABLE_SEMANTIC_SEARCH=true

# Required for background operations (auto-enabled by semantic search)
TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db

# Vector database
QDRANT_URL=http://qdrant:6333

# Embedding provider
OLLAMA_BASE_URL=http://ollama:11434
```

> **Note:** In multi-user modes (Login Flow v2, Multi-User BasicAuth), enabling `ENABLE_SEMANTIC_SEARCH` automatically enables background operations and refresh token storage. You don't need to set `ENABLE_BACKGROUND_OPERATIONS` separately!

### Indexing a subset of mail — `MAIL_INDEX_TAG`

Mail is indexed for semantic search whenever vector sync is on. By default that
means **every message in every mailbox** (up to 100 per mailbox — the Mail API's
per-request maximum). `MAIL_INDEX_TAG` narrows it to the messages a user has
tagged, the mail analogue of `VECTOR_SYNC_TAG` for files.

| Value | Behaviour |
|-------|-----------|
| `""` (default) | Index every message. Unchanged from before this setting existed. |
| a tag display name | Index only messages carrying that Mail tag. |

```dotenv
MAIL_INDEX_TAG=vector-index
```

The tag is created for each user automatically on the first scan, so it shows up
in their Mail tag picker — applying it there **is** the opt-in. Tag names
normalise (`"AI Index"`, `"ai index"` and `"ai_index"` are one tag), and tagging
follows a message's `Message-ID`, so it applies to copies in other mailboxes too.

Notes:

- **Turning it on makes untagged mail unsearchable immediately.** Verify-on-read
  drops untagged results on the next query; the index storage is reclaimed within
  a couple of scan cycles by the usual grace-period eviction. No purge needed.
- **Turning it off is lossy.** Messages indexed *because* they were tagged but
  older than the unfiltered 100-message window fall out of discovery and are
  evicted, then re-embedded if they come back. "Just unset it" is not free.
- **The cap becomes 100 *tagged* messages per mailbox**, which reaches much
  further back in a mailbox than the unfiltered window does — that coverage is
  the main win, beyond simply indexing less. Exceeding it logs a warning naming
  the mailbox.
- If the tag can't be resolved (Mail app down), the mail scan is skipped for that
  cycle rather than falling back to indexing everything, and search keeps
  returning already-indexed mail unverified.
- Max 128 characters (validated at startup — the Mail app rejects longer names).

### Per-document keyword vs hybrid indexing — `VECTOR_SYNC_KEYWORD_TAG`

Documents are indexed **hybrid** (dense semantic + BM25 sparse) or
**keyword-only** (BM25 sparse) **per document**, chosen by which Nextcloud system
tag the file carries (ADR-031). Both live in **one collection** and are returned
by a single unified search.

| Tag | Env var (override the tag name) | Mode | Embedding endpoint |
|-----|---------|------|--------------------|
| `vector-index` | `VECTOR_SYNC_TAG` (default `vector-index`) | hybrid (dense + BM25 sparse) | **Required** (Ollama/Bedrock/OpenAI/Mistral/gateway) |
| `keyword-index` | `VECTOR_SYNC_KEYWORD_TAG` (default `keyword-index`) | keyword (BM25 sparse only) | **None** for those docs |

Both tags are **on by default** — create the `vector-index` and/or `keyword-index`
system tag in Nextcloud and apply it; no env var needed. The env vars only
**rename** a tag, or set `VECTOR_SYNC_KEYWORD_TAG=""` to disable the keyword tag.

Tag a PDF `keyword-index` to lexically index it **without** paying embedding
cost; tag it `vector-index` to also get conceptual/semantic matching. **Hybrid
wins** if a file carries both tags. Discovery is PDF-only, mirroring the
`vector-index` path (tagged folders expand to their PDF descendants).

```dotenv
ENABLE_SEMANTIC_SEARCH=true
QDRANT_URL=http://qdrant:6333
# Both tags work out of the box; vector-index (hybrid) needs an embedding
# endpoint, e.g.:
OLLAMA_BASE_URL=http://ollama:11434
```

Notes:

- Requires `ENABLE_SEMANTIC_SEARCH=true` (both tags use the Qdrant index).
- **Unified search:** `nc_semantic_search` fuses dense + sparse. Keyword-only
  documents contribute only their BM25 (sparse) match, so they appear in
  bm25/hybrid results and are naturally absent from a pure-`semantic` query.
- **Hybrid requires embeddings:** a `vector-index` document whose embedding
  endpoint is unavailable **errors and retries** (then dead-letters) rather than
  silently degrading to keyword-only. Only the `keyword-index` tag produces
  sparse-only points.
- **Fully airgapped:** the `keyword-index` tag is on by default, so just configure
  **no** embedding provider and tag everything `keyword-index` — nothing ever
  contacts an embedding endpoint (the local `SimpleProvider` only sizes the
  dense slot the keyword points never populate). Note the collection is always
  dense-sized from the *configured* provider, so if you set e.g. `OLLAMA_BASE_URL`
  while intending to use only the keyword tag, collection creation still probes
  that provider's dimension at startup — leave the provider env unset for a truly
  offline stack.
- **Retagging** a file between the two tags (unchanged content) reprocesses it:
  keyword→`vector-index` adds a dense vector; the reverse is absorbed by the
  dedup no-downgrade rule while any user still holds `vector-index`.
- **Provisioning:** the `keyword-index` tag is created/exposed by the Astrolabe
  Nextcloud app (or `occ tag:add` / the server's own `get_or_create_tag`).

### Qdrant Vector Database Modes

The server supports three Qdrant deployment modes:

1. **In-Memory Mode** (Default) - Simplest for development and testing
2. **Persistent Local Mode** - For single-instance deployments with persistence
3. **Network Mode** - For production with dedicated Qdrant service

#### 1. In-Memory Mode (Default)

No configuration needed! If neither `QDRANT_URL` nor `QDRANT_LOCATION` is set, the server defaults to in-memory mode:

```dotenv
# No Qdrant configuration needed - defaults to :memory:
ENABLE_SEMANTIC_SEARCH=true
```

**Pros:**
- Zero configuration
- Fast startup
- Perfect for testing

**Cons:**
- Data lost on restart
- Limited to available RAM

#### 2. Persistent Local Mode

For single-instance deployments that need persistence without a separate Qdrant service:

```dotenv
# Local persistent storage
QDRANT_LOCATION=/app/data/qdrant  # Or any writable path
ENABLE_SEMANTIC_SEARCH=true
```

**Pros:**
- Data persists across restarts
- No separate service needed
- Suitable for small/medium deployments

**Cons:**
- Limited to single instance
- Shares resources with MCP server

#### 3. Network Mode

For production deployments with a dedicated Qdrant service:

```dotenv
# Network mode configuration
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=your-secret-api-key  # Optional
QDRANT_COLLECTION=nextcloud_content  # Optional
ENABLE_SEMANTIC_SEARCH=true
```

**Pros:**
- Scalable and performant
- Can be shared across multiple MCP instances
- Supports clustering and replication

**Cons:**
- Requires separate Qdrant service
- More complex deployment

### Qdrant Collection Naming

Collection names are automatically generated to include the embedding model, ensuring safe model switching and preventing dimension mismatches.

#### Auto-Generated Naming (Default)

**Format:** `{deployment-id}-{model-name}`

**Components:**
- **Deployment ID:** `OTEL_SERVICE_NAME` (if configured) or `hostname` (fallback)
- **Model name:** `OLLAMA_EMBEDDING_MODEL`

**Examples:**

```bash
# With OTEL service name configured
OTEL_SERVICE_NAME=my-mcp-server
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
# → Collection: "my-mcp-server-nomic-embed-text"

# Simple Docker deployment (OTEL not configured)
# hostname=mcp-container
OLLAMA_EMBEDDING_MODEL=all-minilm
# → Collection: "mcp-container-all-minilm"
```

#### Switching Embedding Models

When you change `OLLAMA_EMBEDDING_MODEL`, a new collection is automatically created:

```bash
# Initial setup
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
# Collection: "my-server-nomic-embed-text" (768 dimensions)

# Change model
OLLAMA_EMBEDDING_MODEL=all-minilm
# Collection: "my-server-all-minilm" (384 dimensions)
# → New collection created, full re-embedding occurs
```

**Important:**
- **Collections are mutually exclusive** - vectors cannot be shared between different embedding models
- **Switching models requires re-embedding** all documents (may take time for large note collections)
- **Old collection remains** in Qdrant and can be deleted manually if no longer needed

#### Startup migrations on existing collections

On the first call to `get_qdrant_client()` against an existing collection, the
server runs two idempotent migrations:

1. **Payload-index creation** — adds `KEYWORD` payload indexes for `doc_id`,
   `user_id`, and `doc_type`. Required by Qdrant for any `FieldCondition`
   filter. Cheap; runs even on healthy collections.
2. **`doc_id` backfill** — scans the collection once and rewrites any
   legacy integer `doc_id` payloads to strings so they match the keyword
   index. Idempotent: on a clean collection (all `doc_id` values already
   `str`), the scroll runs but emits zero writes. On the first start after
   the upgrade, expect a delay proportional to total point count for the
   scroll itself, plus an additional delay proportional to any `int`-typed
   `doc_id` points found while their payloads are rewritten.

Both steps emit INFO-level log lines so operators can track progress.

> **Operator note:** if the server logs `TypeError: SemanticSearchResult.id
> must be int-convertible` after upgrading, this indicates a `doc_type`
> with non-numeric ids has been indexed but the public response model
> (`SemanticSearchResult.id: int`) has not been widened to accept strings.
> Semantic search itself is not broken — the boundary cast in
> `server/semantic.py` is failing loudly on purpose so the discrepancy is
> caught early. Either widen the public model's `id` field or convert the
> id at the verifier layer.

> **Degraded-migration signals:** both startup steps swallow non-fatal
> failures so the server still starts, but each leaves a distinct ERROR
> log line that operators should treat as a "restart needed" signal:
>
> - `Unexpected error creating payload index on '<field>' (status 5xx)` —
>   the index was not created. Searches filtering on that field will keep
>   returning HTTP 400 (`Index required but not found`) until a subsequent
>   restart succeeds in creating it.
> - `doc_id backfill scroll failed on '<collection>'; will retry on next restart` —
>   the migration sentinel was not written. Legacy integer `doc_id`
>   payloads remain invisible to the keyword index in the meantime; the
>   scroll re-runs from scratch on the next process start.
>
> Neither prevents the server from accepting requests, but both indicate
> that vector search is operating in a degraded state on the affected
> collection until the next clean restart.

#### Explicit Override

Set `QDRANT_COLLECTION` to use a specific collection name:

```bash
QDRANT_COLLECTION=my-custom-collection  # Bypasses auto-generation
```

**Use cases:**
- Backward compatibility with existing deployments
- Custom naming schemes
- Sharing a collection across deployments (advanced)

#### Multi-Server Deployments

Each server should have a unique deployment ID to avoid collection collisions:

```bash
# Server 1 (Production)
OTEL_SERVICE_NAME=mcp-prod
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
# → Collection: "mcp-prod-nomic-embed-text"

# Server 2 (Staging)
OTEL_SERVICE_NAME=mcp-staging
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
# → Collection: "mcp-staging-nomic-embed-text"

# Server 3 (Different model)
OTEL_SERVICE_NAME=mcp-experimental
OLLAMA_EMBEDDING_MODEL=bge-large
# → Collection: "mcp-experimental-bge-large"
```

**Benefits:**
- Multiple MCP servers can share one Qdrant instance safely
- No naming collisions between deployments
- Clear collection ownership (can see which deployment and model)

#### Dimension Validation

The server validates collection dimensions on startup:

```
Dimension mismatch for collection 'my-server-nomic-embed-text':
  Expected: 384 (from embedding model 'all-minilm')
  Found: 768
This usually means you changed the embedding model.
Solutions:
  1. Delete the old collection: Collection will be recreated with new dimensions
  2. Set QDRANT_COLLECTION to use a different collection name
  3. Revert OLLAMA_EMBEDDING_MODEL to the original model
```

**What this prevents:**
- Runtime errors from dimension mismatches
- Data corruption in Qdrant
- Confusing error messages during indexing

### Background Indexing Configuration

Control background indexing behavior:

```dotenv
# Semantic search (ADR-007, ADR-021)
ENABLE_SEMANTIC_SEARCH=true           # Enable background indexing

# Tuning parameters (advanced - only modify if needed)
VECTOR_SYNC_SCAN_INTERVAL=300         # Scan interval in seconds (default: 5 minutes)
VECTOR_SYNC_PROCESSOR_WORKERS=3       # Concurrent indexing workers (default: 3)
# Optional per-tier concurrency overrides (unset = inherit PROCESSOR_WORKERS).
# Precedence: worker --concurrency flag > tier override > PROCESSOR_WORKERS.
VECTOR_SYNC_FAST_CONCURRENCY=         # Fast-tier worker concurrency (default: unset)
VECTOR_SYNC_STRUCTURED_CONCURRENCY=   # Structured-tier worker concurrency (default: unset)
VECTOR_SYNC_QUEUE_MAX_SIZE=10000      # Max queued documents (default: 10000)

# Document chunking settings (for vector embeddings)
DOCUMENT_CHUNK_SIZE=2048              # Characters per chunk (default: 2048)
DOCUMENT_CHUNK_OVERLAP=200            # Overlapping characters between chunks (default: 200)
DOCUMENT_CHUNK_PAGE_PACK=false        # Merge consecutive sub-budget PDF pages into one chunk (default: false)
CHUNKING_CONFIG_VERSION=1             # Chunker config generation; bump on any chunker behaviour change (default: 1)
```

> **Note:** The `VECTOR_SYNC_*` tuning parameters keep their names as they're implementation details. Only the user-facing feature flag was renamed to `ENABLE_SEMANTIC_SEARCH`.

#### Reading documents — a per-call decision, not a server switch

**There is no master switch.** Whether to extract text from a document is decided
by the caller, per read, through `nc_webdav_read_file`'s `parse_document`
argument — the server cannot know whether an agent wants a PDF's text or its
bytes. (`ENABLE_DOCUMENT_PROCESSING` was removed in **0.151.0**; it is ignored if
still set.)

| `parse_document` | What you get |
|---|---|
| `"auto"` (default) | The document's text via the tiered pipeline: `fast` (text layer) → `structured` or `ocr` when the classifier says the text layer is unusable. |
| `"markdown"` | Reconstructed structure (headings, tables) via the `structured` tier, bounded by `DOCUMENT_MARKDOWN_MAX_PAGES`. |
| `"raw"` | No parse: text files decoded, anything else base64. |

The built-in PDF tiers (`pypdfium2_fast` → `fast`, `pymupdf` → `structured`, and
the `ocr` tier) are always available. The optional processors — `unstructured`,
`tesseract`, `custom`, **docling** — are each registered from their own
`ENABLE_*` flag at startup; configuring none of them means the parse stack is not
even imported.

Every read reports what it actually produced, so a degraded extraction is never
presented as the whole document:

- `parse_status` — `parsed` / `failed` / `skipped` / `not_applicable`
- `parse_tier`, `parse_processor` — which tier produced the content
- `content_format` — `markdown`, `text` or `base64`
- `parse_notes` — plain statements when something degraded: OCR not enabled,
  markdown structure not reconstructed (with the page count and the ceiling), the
  size cap, a failed or timed-out parse.

What still bounds an interactive read: `DOCUMENT_MAX_PDF_SIZE_MB` (parse cap; the
download is aborted at twice that), `DOCUMENT_READ_TIMEOUT_SECONDS` (wall-clock
cap on the synchronous parse), `DOCUMENT_OCR_ENABLED` (the expensive tier stays
opt-in) and `DOCUMENT_MARKDOWN_MAX_PAGES=0` (disables markdown outright). The
document is streamed to a spool file and parsed from there, so a large read does
not scale the server's memory.

#### Document parsing robustness (PDF)

These guard the parse/OCR tiers against pathological PDFs. Defaults are safe;
tune per tenant when a corpus has very large scans or a gateway with its own
shorter OCR ceiling:

```dotenv
DOCUMENT_PARSE_TIMEOUT_SECONDS=120    # Wall-clock cap per isolated parse (default: 120)
DOCUMENT_OCR_TIMEOUT_SECONDS=180      # OCR backend request timeout (default: 180)
DOCUMENT_MAX_PDF_SIZE_MB=50           # Pre-parse size cap; 0 disables (default: 50)
DOCUMENT_PARSE_PAGE_WINDOW=100        # Pages per extraction window; 0 disables (default: 100)
DOCUMENT_PARSE_PROCESS_SLOTS=2        # Concurrent isolated parse subprocesses (default: 2)
DOCUMENT_MARKDOWN_MAX_PAGES=150       # Structured-tier markdown page ceiling; 0 disables markdown (default: 150)
PYMUPDF_EXTRACT_IMAGES=true           # Extract embedded images during markdown reconstruction (default: true)
PYMUPDF_IMAGE_DIR=                    # Where extracted images are written; empty = system temp dir
```

`PYMUPDF_*` tune the built-in `structured` tier, which is always available — there
is no enable flag for it. Image extraction only happens on the markdown path, so
a document past `DOCUMENT_MARKDOWN_MAX_PAGES` writes no images regardless.

`DOCUMENT_PARSE_PROCESS_SLOTS` bounds how many isolated parse subprocesses run at
once. Without it anyio defaults to an `os.cpu_count()`-wide pool, which is
constrained by neither the worker's `--concurrency` nor the pod memory limit: on
an 8-core node that permits `8 × DOCUMENT_PARSE_MEM_LIMIT_MB` (~12 GiB of address
space) inside a 3 GiB pod. `RLIMIT_AS` caps virtual address space rather than
resident memory, so that is a ceiling rather than a reservation — but it is still
well beyond what the pod can survive. Keep
`DOCUMENT_PARSE_PROCESS_SLOTS × DOCUMENT_PARSE_MEM_LIMIT_MB` within the pod's
memory limit. The limiter is created once per worker, so a change needs a restart.

A PDF larger than `DOCUMENT_MAX_PDF_SIZE_MB` fails fast with reason `oversize`
(exported on `astrolabe_document_parse_failed_total{reason="oversize"}`) instead
of being handed to the tiers, where a 40+ MB scan would otherwise burn the full
OCR timeout for zero recovered text.

**Sizing the cap for a tenant.** Two metrics make the corpus visible instead of
requiring a manual crawl:

- `astrolabe_document_ingest_size_bytes{doc_type}` — a histogram of source sizes,
  observed **before** the cap is applied, so the over-cap tail is included.
  Buckets run to 2 GiB.
- `astrolabe_document_ingest_rejected_total{doc_type,reason="oversize"}` — how
  many documents the cap turned away.

The fraction of a tenant's corpus blocked by the cap is then a query rather than
an investigation, e.g.:

```promql
sum(rate(astrolabe_document_ingest_rejected_total{reason="oversize"}[1h]))
  / sum(rate(astrolabe_document_ingest_size_bytes_count[1h]))
```

> **Changing `DOCUMENT_MAX_PDF_SIZE_MB` re-drives dead-lettered documents.** The
> cap is part of the escalation-tier signature that keys the document dead-letter
> marker, so raising it makes previously-oversize documents retryable without
> waiting for their etag to change (which, for an archive of scanned documents,
> never happens). The trade-off is that a cap change invalidates *all* dead
> letters for the tenant, not just oversize ones, so genuinely corrupt files are
> re-attempted once too. On a large tenant that is a thundering herd — roll the
> change out one tenant at a time and watch ingest queue depth.

#### Markdown page ceiling (structured tier)

`DOCUMENT_MARKDOWN_MAX_PAGES` bounds the **structured** tier. Above it, the tier
skips `pymupdf4llm.to_markdown` and returns the raw text layer instead; `0`
disables markdown entirely. A negative value is rejected at startup, so a typo
cannot quietly turn markdown off across the fleet.

`to_markdown` is **superlinear in page count** — the per-page rate itself grows
with document size. Measured across an 866-file corpus of scanned documents:

| Pages | to_markdown | Whole document |
|---|---|---|
| 22–31 | 0.48–1.48 s/page | 13–33 s |
| 136–158 | 0.60–0.94 s/page | 95–149 s |
| 364–419 | 0.91–1.06 s/page | 331–444 s |
| 1111–1898 | 1.48–3.11 s/page | 27–98 min |
| 4003 | 5.92 s/page | **6.6 hours** |

Raw `get_text` is ~4.5 ms/page and flat. On that corpus it recovered 116,375 of
the 145,199 characters markdown produced — markdown's value is structure, not
completeness.

Without a ceiling, a large document burns the whole
`DOCUMENT_PARSE_TIMEOUT_SECONDS` and then dead-letters `reason="timeout"`,
discarding a text layer that was extractable in under a second.

The gate is expressed in **pages rather than predicted seconds** deliberately:
seconds depend on node CPU, so a seconds-based threshold drifts silently between
node types and needs recalibration, while a page count is deterministic and
reviewable. Pick it from the tier's real budget — at ~1 s/page on a throttled
2-core pod, a 120 s timeout is roughly 120 pages.

Above the ceiling no images are written either, since markdown reconstruction is
what emits them — so `has_images` is `False` for a gated document even when the
processor was constructed with `extract_images=True`.

Which path ran is exported on `astrolabe_document_parse_mode_total{mode}`
(`markdown` | `text_only`) and recorded on the result as `parse_mode`. Skipping
markdown is a **successful** parse, so it is deliberately not counted as a parse
failure:

```promql
sum by (mode) (rate(astrolabe_document_parse_mode_total[1h]))
```

> **Changing `DOCUMENT_MARKDOWN_MAX_PAGES` also re-drives dead letters.** Like
> the size cap it is part of the escalation-tier signature: lowering it lets a
> previously-timing-out document take the raw-text path and succeed, so the
> documents dead-lettered under the old value must become retryable. The same
> thundering-herd caveat applies.

`DOCUMENT_PARSE_PAGE_WINDOW` bounds the **fast** tier's peak memory. PDFium keeps
parsed page objects for the lifetime of the open document and `page.close()` does
not give them back, so extracting a long document in one open makes peak RSS scale
with page count — measured at ~0.5 MB/page, i.e. 1.9 GB for a real 4003-page
document, enough to OOM a 3 GiB worker on its own. The extractor therefore
re-opens the document every `DOCUMENT_PARSE_PAGE_WINDOW` pages; the freed arena is
reused by the next window, so peak stays flat at roughly one window's worth
(100 pages ≈ 85 MB) with byte-identical output and no measurable slowdown.

Lower it for very memory-constrained workers, raise it to trade memory for fewer
re-opens. Note the cost is per *page*, not per byte — a 500 MB / 70-page scan is
far cheaper than a 100 MB / 4000-page one, so page count, not file size, is what
this setting tracks.

#### OCR tier configuration

OCR is a single configurable escalation tier. Enable it and choose the backend,
model, and execution mode:

```dotenv
DOCUMENT_OCR_ENABLED=true              # route scanned/no-text-layer PDFs to OCR (default: false)
DOCUMENT_OCR_PROVIDER=auto            # "auto" | "gateway" | "mistral" | "docling" | "none"
DOCUMENT_OCR_MODEL=mistral/mistral-ocr-latest  # provider-namespaced model id
```

- **`DOCUMENT_OCR_PROVIDER`** selects the backend: `gateway` posts to the Astrolabe
  Cloud model gateway's `POST /v1/ocr` (no provider keys in the pod; the gateway
  routes on the model's `<provider>/` prefix, so it serves Mistral, surya, etc.);
  `mistral` calls the Mistral OCR API directly (`MISTRAL_API_KEY`); `docling`
  posts scanned/no-text-layer PDFs to a self-hosted docling-serve instance
  (`DOCLING_API_URL`); `auto` prefers the gateway (if `EMBEDDING_GATEWAY_URL` is
  set) then direct Mistral (`auto` never selects docling — it needs an explicit
  self-hosted URL); `none` disables OCR.
- **`DOCUMENT_OCR_MODEL`** is the provider-namespaced model id — e.g.
  `mistral/mistral-ocr-latest` (Mistral) or `surya/surya-ocr-2` (surya, via the
  gateway). The gateway routes on the prefix; the direct Mistral backend strips it.
  (Ignored by the `docling` backend, which uses the docling-serve instance's own
  OCR engine.)

#### Docling (docling-serve) — photographed / scanned / handwritten text

[docling](https://github.com/docling-project/docling) has notably stronger OCR
than `unstructured` for photographed, scanned and **handwritten** documents. The
MCP server talks to an external
[docling-serve](https://github.com/docling-project/docling-serve) instance over
HTTP — no ML dependencies are added to the server image. Run one via the
`docling` docker-compose profile (`docker compose --profile docling up -d`).

```dotenv
ENABLE_DOCLING=false                  # master switch for the docling touchpoints
DOCLING_API_URL=http://docling:5001   # docling-serve base URL (required)
DOCLING_TIMEOUT=120                   # INTERACTIVE image/force read timeout (nc_webdav_read_file); keep client-friendly
DOCLING_OCR_LANG=en,de                # engine-dependent codes (EasyOCR: en,de; Tesseract: eng,deu)
DOCLING_DO_OCR=true                   # run OCR (vs. text-layer extraction only)
DOCLING_PIPELINE=standard             # "standard" (classic OCR) | "vlm" (vision-language model)
DOCLING_VLM_PRESET=                   # VLM preset name when DOCLING_PIPELINE=vlm (unset = docling-serve default)
DOCUMENT_READ_TIMEOUT_SECONDS=        # opt-in cap on the interactive read parse; empty = disabled (see VLM note)
```

**Required configuration per use case** (`auto` never selects docling — it needs
an explicit self-hosted URL):

| Use case | Minimal env |
|---|---|
| Images auto-route to docling | `ENABLE_DOCLING=true` + `DOCLING_API_URL` |
| Scanned / no-text-layer PDFs auto-OCR via docling | `DOCUMENT_OCR_ENABLED=true` + `DOCUMENT_OCR_PROVIDER=docling` + `DOCLING_API_URL` |
| **VLM** for bulk PDF indexing (async, recommended) | scanned-PDF row + `DOCLING_PIPELINE=vlm` (+ `DOCLING_VLM_PRESET`) + raise `DOCUMENT_OCR_TIMEOUT_SECONDS` (e.g. 600–900) |
| **VLM** for interactive image reads | image row + `DOCLING_PIPELINE=vlm` (+ `DOCLING_VLM_PRESET`); expect long blocking — see the VLM note below |

The scanned-PDF row omits `ENABLE_DOCLING`: that path rides the
always-registered `ocr` tier rather than the standalone image processor. When it
runs during **indexing** it also needs `ENABLE_SEMANTIC_SEARCH=true`; on an
interactive `nc_webdav_read_file` it needs nothing further.

Docling plugs in at two points — it is an **OCR provider**, never a tier of its
own, so a PDF reaches it through the OCR tier and nowhere else:

- **Images (automatic).** With `ENABLE_DOCLING=true` + `DOCLING_API_URL` set,
  image files (`image/jpeg`, `image/png`, `image/tiff`, `image/bmp`, `image/gif`,
  `image/webp`) always route to docling — it registers at a higher priority than
  `unstructured`. `DOCLING_DO_OCR` toggles OCR on this image path only (the scanned-PDF
  OCR backend always OCRs). If `DOCLING_API_URL` is unset the processor is
  not registered, so a bare `ENABLE_DOCLING` never shadows other image processors
  with a dead endpoint.
- **Scanned PDFs (automatic).** Set `DOCUMENT_OCR_ENABLED=true` +
  `DOCUMENT_OCR_PROVIDER=docling`. PDFs whose text layer the tier-0 classifier
  finds missing/unusable escalate to the OCR tier and are transcribed by docling.
  Born-digital (text-layer) PDFs still use the cheap local `fast`/`structured`
  tiers — docling is only paid for genuine scans. This applies to both indexing
  and an interactive `nc_webdav_read_file`.

  For a text-layer PDF whose layer misses tables or figures, ask the read for
  `parse_document="markdown"`: the `structured` tier (pymupdf4llm) reconstructs
  table structure locally, without a docling round-trip. (Before 0.151.0 this was
  `force_processor="docling"`, which asked the caller to name an extraction
  engine it had no basis to choose.)

Office formats (DOCX/PPTX/XLSX) deliberately stay with `unstructured` — docling
is scoped to the image/scan/handwriting use case here. OCR language codes are
engine-dependent: the docling-serve default engine (EasyOCR) uses two-letter
codes (`en,de`); a Tesseract-backed instance wants `eng,deu`. The synchronous
convert endpoint has an observed ~2 min practical ceiling (from our testing, not a
hard server-enforced limit), so a larger `DOCLING_TIMEOUT` (e.g. 300s for slow CPU
OCR) simply lets a slow conversion finish; very large scans are future work (async
submit/poll). See `docs/ADR-031-docling-document-parsing-backend.md`.

**VLM pipeline (opt-in).** docling-serve can also transcribe with a
vision-language model instead of classic OCR — often markedly better on messy
scans, handwriting and complex layouts. The pipeline is **client-selected**: set
`DOCLING_PIPELINE=vlm` and the docling client sends `pipeline=vlm` (plus
`DOCLING_VLM_PRESET`, if set, and a lean `image_export_mode=placeholder`) on
**both** the image and scanned-PDF touchpoints. Presets are defined by the
docling-serve instance (e.g. `glm_ocr` backed by a local Ollama), so the client
does not validate the name — an unknown preset surfaces as a docling error. Under
`vlm` the classic `DOCLING_DO_OCR`/`DOCLING_OCR_LANG` knobs are inert and not sent.
The default `standard` is byte-identical to the pre-VLM request, so leaving it
unset changes nothing. The chosen pipeline is recorded in
`parsing_metadata.docling_pipeline` while `parsing_method` stays `docling`.

**VLM is much slower than classic OCR (~90–200s/page), so where you run it
matters — and the two touchpoints have independent timeouts:**

- **Bulk indexing (recommended for VLM):** with `DOCUMENT_OCR_PROVIDER=docling`,
  scanned PDFs are transcribed on the **async ingest pipeline** (`mcp_role=worker`,
  the `ingest-ocr` queue) and written to the search index. That path uses
  **`DOCUMENT_OCR_TIMEOUT_SECONDS`** and never blocks a tool call — raise it freely
  (e.g. 600–900s) for VLM.
- **Interactive reads (`nc_webdav_read_file` on images):**
  these parse **synchronously** and block for up to **`DOCLING_TIMEOUT`**. Raising
  `DOCLING_TIMEOUT` for VLM directly lengthens that block, and MCP clients usually
  enforce a much shorter per-tool timeout (~30–60s) — so the client typically kills
  the call before docling responds and you see a client timeout, not the tool's
  base64 fallback. **Do not inflate `DOCLING_TIMEOUT` to force interactive VLM.**
  Images are interactive-only (the ingest scanner is PDF-only), so interactive VLM
  image reads inherently block.

**`DOCUMENT_READ_TIMEOUT_SECONDS` (opt-in cap).** Set it to bound the synchronous
parse inside `nc_webdav_read_file` (via `anyio.fail_after`), independent of
`DOCLING_TIMEOUT` and of the worker path: when the cap trips, the tool returns
base64 **fast** instead of hanging until the client times out. Default is empty
(disabled) — no behavior change for existing reads. Set a client-friendly bound
(e.g. 45–60s) if you want graceful fallback; leave it unset (and expect long calls)
if you deliberately want interactive VLM with a tolerant client. It never affects
the async ingest/worker path. See `docs/ADR-032-docling-vlm-pipeline.md`.

#### OCR execution mode: synchronous vs batch (Deck #332)

The OCR tier has two execution modes, selected by `DOCUMENT_OCR_MODE`:

```dotenv
DOCUMENT_OCR_MODE=sync                 # "sync" (default) | "batch"
DOCUMENT_OCR_BATCH_POLL_SECONDS=120    # re-poll cadence for a batch job (default: 120)
```

- **`sync`** (default) — transcribe the document inline via the backend's
  synchronous path (`POST /v1/ocr` for the gateway, or the direct Mistral OCR
  API). The document is parsed in a single call.
- **`batch`** — submit the document to the **gateway's async Batch OCR** job
  (`POST /v1/ocr/batch`) and re-poll `GET /v1/ocr/batch/{job_id}` until it
  finishes. This trades latency (a batch job runs minutes–hours) for roughly
  **half the OCR cost**, so it suits large-corpus backfill rather than
  interactive ingest.

Batch mode is **opt-in and gateway-routed**: the embedding gateway is the
batching layer, so batch OCR always routes *through* the gateway's batch routes
(no provider keys in the pod). This is how batch works even when the chosen *sync*
backend is direct Mistral — we leverage the gateway to batch for backends that
have no native batch path from the pod. Because it needs a gateway, `DOCUMENT_OCR_MODE=batch`
**requires `EMBEDDING_GATEWAY_URL`**: the server rejects the combination at startup
rather than silently downgrading to synchronous OCR. Batch also requires the
Postgres ingest queue (the per-tier procrastinate workers); the in-process
(`INGEST_QUEUE=memory`) pipeline can't defer a poll, so use `DOCUMENT_OCR_MODE=sync`
there.

Mechanics: the OCR tier submits the job, records its id in the `batch_ocr_jobs`
app-DB table (keyed on the document + its etag), and raises a re-poll deferral so
procrastinate re-runs the tier after `DOCUMENT_OCR_BATCH_POLL_SECONDS` (or the
gateway's `Retry-After` when longer, so a large pending backlog can't storm it —
capped at an internal 1h ceiling, `_BATCH_POLL_MAX_DEFER_SECONDS`, or the poll
interval if that is set higher, so a malformed/absurd header can't stall a poll
unboundedly) —
releasing the worker slot between polls (a long batch never pins a worker or is
reclaimed as stalled). On completion the per-page markdown is indexed exactly like
the sync path. A pending job is polled **indefinitely**: once the gateway accepts a
document it owns the OCR lifecycle (Deck #523), so there is no worker-side give-up
deadline — a transient backend/GPU outage only delays completion, never fails the
document. Only a job-level failure (or a per-document error inside a succeeded job)
marks the document parse-failed. Each poll re-fetches + re-classifies the PDF (a known v1
inefficiency, bounded by the poll cadence); one batch job is submitted per
document (coalescing many documents per job is a planned follow-up).

#### Upgrade notes: OCR-tier consolidation

The two OCR rungs (`ocr-incluster` / `ocr-upstream`) were merged into one `ocr`
tier. When upgrading from a deployment that used the split tiers:

- **`DOCUMENT_OCR_INCLUSTER_ENABLED` / `DOCUMENT_OCR_INCLUSTER_MODEL` are removed.**
  Configure the single tier with `DOCUMENT_OCR_PROVIDER` + `DOCUMENT_OCR_MODEL`
  (the gateway routes on the model's `<provider>/` prefix, so surya is reached by
  setting e.g. `DOCUMENT_OCR_PROVIDER=gateway` + `DOCUMENT_OCR_MODEL=surya/surya-ocr-2`).
- **Dead-letter retry burst.** The dead-letter signature dropped its `ocric=`
  component, so a deployment that had `DOCUMENT_OCR_INCLUSTER_ENABLED=true` will see
  its signature change on upgrade and **automatically re-attempt** documents that
  were dead-lettered while the in-cluster rung was on. This is intended (those docs
  can OCR via the unified tier) but can produce a burst of OCR jobs on
  scanned-doc-heavy instances right after deploy — expect it and scale the OCR
  worker fleet accordingly.
- **Batch mode now fails loud instead of downgrading to sync.** `DOCUMENT_OCR_MODE=batch`
  requires the embedding gateway (rejected at startup without `EMBEDDING_GATEWAY_URL`)
  and the Postgres ingest queue. On the in-process (`INGEST_QUEUE=memory`) path —
  which can't defer a poll — batch raises at ingest time rather than silently
  transcribing synchronously; use `DOCUMENT_OCR_MODE=sync` there.
- **In-flight jobs are drained.** Jobs still parked on the old `ingest-ocr-incluster`
  / `ingest-ocr-upstream` queues during a rolling upgrade are processed by the new
  `ocr` worker and routed back to the single `ocr` tier — nothing is stranded.

### Embedding Service Configuration

The server picks an embedding provider via auto-detection. Priority order
(see `nextcloud_mcp_server/providers/registry.py`):

1. **Bedrock** — if `AWS_REGION` or `BEDROCK_EMBEDDING_MODEL` is set
2. **OpenAI** — if `OPENAI_API_KEY` is set
3. **Mistral** — if `MISTRAL_API_KEY` is set
4. **Ollama** — if `OLLAMA_BASE_URL` is set
5. **Simple** — fallback when nothing else is configured

#### Ollama (Recommended for self-hosted)

Use a local Ollama instance for embeddings:

```dotenv
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text  # Default model
OLLAMA_VERIFY_SSL=true                   # Verify SSL certificates
```

#### OpenAI

Hosted OpenAI embeddings (or any OpenAI-compatible API via `OPENAI_BASE_URL`):

```dotenv
OPENAI_API_KEY=sk-...
OPENAI_EMBEDDING_MODEL=text-embedding-3-small  # default
# OPENAI_BASE_URL=https://models.github.ai/inference  # optional
```

#### Mistral

Hosted Mistral embeddings. Requires a Mistral API key from
[console.mistral.ai](https://console.mistral.ai). Currently embeddings only
(no text generation).

```dotenv
MISTRAL_API_KEY=...
MISTRAL_EMBEDDING_MODEL=mistral-embed   # default; produces 1024-dim vectors
# MISTRAL_BASE_URL=https://api.mistral.ai  # optional override (proxies, on-prem)
```

Switching to or from Mistral forces a new Qdrant collection because the
collection name encodes the model (see "Qdrant Collection Naming" above).

#### Amazon Bedrock

Bedrock provides hosted embedding models (Titan, Cohere) and uses the AWS
credential chain (env vars, profiles, or IAM role):

```dotenv
AWS_REGION=us-east-1
BEDROCK_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are optional — boto3 will use
# the standard credential chain if not set.
```

#### Simple Embedding Provider (Fallback)

If no provider env var is set, the server falls back to a simple deterministic
embedding provider for testing. This is **not suitable for production** as
its embeddings have no semantic meaning.

```dotenv
SIMPLE_EMBEDDING_DIMENSION=384  # optional; default 384
```

### Document Chunking Configuration

The server chunks documents before embedding to handle documents larger than the embedding model's context window. Chunk size and overlap can be tuned based on your embedding model and content type.

#### Choosing Chunk Size

**Smaller chunks (1024-1536 characters)**:
- More precise matching
- Less context per chunk
- Better for finding specific information
- Higher storage requirements (more vectors)

**Larger chunks (3072-4096 characters)**:
- More context per chunk
- Less precise matching
- Better for understanding broader topics
- Lower storage requirements (fewer vectors)

**Default (2048 characters)**:
- Balanced approach suitable for most use cases
- Works well with typical note lengths
- Good compromise between precision and context

> For PDFs, `DOCUMENT_CHUNK_PAGE_AWARE` (default `true`) overrides this trade-off by chunking one page at a time — see the entry below.

#### Choosing Overlap

Overlap preserves context across chunk boundaries. Recommended settings:

- **10-20% of chunk size** (e.g., 200-400 characters for 2048-character chunks)
- **Too small** (<10%): May lose context at boundaries
- **Too large** (>20%): Redundant storage, diminishing returns

**Examples**:
```dotenv
# Precise matching for short notes
DOCUMENT_CHUNK_SIZE=1024
DOCUMENT_CHUNK_OVERLAP=100

# Default balanced configuration
DOCUMENT_CHUNK_SIZE=2048
DOCUMENT_CHUNK_OVERLAP=200

# More context for long documents
DOCUMENT_CHUNK_SIZE=4096
DOCUMENT_CHUNK_OVERLAP=400
```

**Important**: Changing chunk size requires re-embedding all documents. The collection naming strategy (see "Qdrant Collection Naming" above) helps manage this by creating separate collections for different configurations.

### Verify-on-Read Latency Budget

Every semantic search request runs an access-control verification pass over its
results before returning them, to filter out documents the user can no longer
access (deleted, unshared, permissions changed). See
[ADR-019](ADR-019-verify-on-read-for-semantic-search.md) for the full design.

This adds Nextcloud round-trips to the search path that operators should be
aware of:

- **Per-search cost**: one Nextcloud round-trip per *unique* `(doc_id, doc_type)`
  in the result set — except `file` and `news_item`, which each batch into a
  single call per search regardless of how many results they contribute (see
  the Files and News caveats below). Chunking means a 10-result page typically
  references 3-5 unique documents, so verification adds 3-5 round-trips. With
  the default 20-way concurrency this is one parallel batch — usually under
  100 ms on a healthy connection.
- **Concurrency**: all verifications fan out under a shared semaphore.
  Tunable via the `VERIFICATION_CONCURRENCY` env var (settings field
  `verification_concurrency`, default 20) — lower it if your Nextcloud
  backend struggles with the parallel fan-out, or raise it on a healthy
  connection to speed up large result pages.
- **News API caveat**: the News app has no per-item endpoint, so the news
  verifier issues a single `news.get_items(batch_size=-1, get_read=True)` call
  per search that contains any news result, then intersects locally. The
  payload is **unbounded** — for users with very large feed backlogs this can
  dominate verification latency. As a rough guide on a healthy LAN connection:
  a typical purged backlog (1k–5k items) returns in ~200–500 ms; very large
  backlogs (>20k items) can exceed 2 s and become the dominant cost of any
  search that surfaces news results. Disabling News in the indexer or running
  with a smaller backlog mitigates this; per-item paginated verification is
  tracked as a future improvement.
- **Files caveat**: `file` results are gated on current **`vector-index` tag
  membership**, not bare access — the verifier issues a single
  `find_files_by_tag(<tag>, mime_type_filter="application/pdf")` REPORT per
  search that contains any file result (plus a one-shot `EXCLUDED_TAGS`
  lookup), then keeps only files in that set. This matches exactly what the
  scanner indexes, so a file removed from the tag (or deleted, or moved under
  an excluded folder) drops out of results immediately rather than waiting for
  the scanner sweep. The REPORT expands tagged folders via a `Depth: infinity`
  SEARCH, so deployments that tag whole directory trees pay that walk once per
  search; configure `VECTOR_SYNC_TAG` to change the tag name. The `file`
  verifier's latency therefore scales with **both** the `Depth: infinity` folder
  expansion **and** the `EXCLUDED_TAGS` lookup: that lookup fans out ~2 WebDAV
  calls (1 PROPFIND + 1 REPORT) *per excluded tag*, concurrently, while holding
  a single verification slot — so a deployment with a long `EXCLUDED_TAGS` list
  and/or deeply tagged trees issues many parallel Nextcloud requests per search.
  Operators in that situation may want to **lower `VERIFICATION_CONCURRENCY`** so
  the file verifier's internal fan-out does not overwhelm the backend.
- **Shared files**: a file an owner tagged and shared with the searcher only
  survives verification if the owner's **`userVisible`** tag surfaces in the
  *searcher's* tag REPORT. The MCP server's own tag-creation path
  (`WebDAVClient.get_or_create_tag`) defaults to `user_visible=True`, so tags it
  creates are fine. **Migration caveat**: if the `vector-index` tag was created
  some other way — manually via `occ tag:add … --user-visible=false`, or in a
  deployment predating this release — it may be `user_visible=False` (the
  Nextcloud default for system-managed tags). In that case an owner's tag will
  **not** surface in a recipient's systemtag REPORT, so every shared-file result
  is *silently dropped* for recipients after upgrading — no error, just a
  narrower result set. Verify the tag's visibility (Administration → *Collaborative
  tags*, or `occ tag:list`) and, if it is not user-visible, recreate it as
  user-visible so shared search keeps working.
- **Eviction**: when verification finds a definitive miss (a 404 / 403, or — for
  files — absence from the tag set), the corresponding Qdrant points are deleted
  in the background on a lifespan-owned task group — fire-and-forget, does
  **not** block the search response. Eviction failures are logged but never
  propagated; the next query will re-verify and re-attempt (self-healing).
- **Failure modes**: transient errors (5xx, network) keep results visible
  (fail open) so a flaky link does not silently shrink result pages; only
  *definitive* misses (404 / 403, or a file no longer in the tag set) drop them.
  If the file tag REPORT itself errors, all file results are kept (fail open).

If eviction ever needs to be disabled (debugging, benchmarking), the
`evict_on_missing=False` keyword argument on `verify_search_results()` skips
the Qdrant deletes without changing what is returned to the caller. **This
is a developer/test flag, not an operator knob — it has no env-var
equivalent.** Operators who need a runtime toggle should open an issue.

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ENABLE_SEMANTIC_SEARCH` | ⚠️ Optional | `false` | Enable semantic search with background indexing (replaces `VECTOR_SYNC_ENABLED`) |
| `VECTOR_SYNC_TAG` | ⚠️ Optional | `vector-index` | Nextcloud tag marking files for **hybrid** (dense + BM25 sparse) indexing (ADR-031) |
| `VECTOR_SYNC_KEYWORD_TAG` | ⚠️ Optional | `keyword-index` | Nextcloud tag marking files for **keyword-only** (BM25 sparse) indexing into the same collection; on by default, set empty to disable. Hybrid wins if a file carries both tags (ADR-031) |
| `QDRANT_URL` | ⚠️ Optional | - | Qdrant service URL (network mode) - mutually exclusive with `QDRANT_LOCATION` |
| `QDRANT_LOCATION` | ⚠️ Optional | `:memory:` | Local Qdrant path (`:memory:` or `/path/to/data`) - mutually exclusive with `QDRANT_URL` |
| `QDRANT_API_KEY` | ⚠️ Optional | - | Qdrant API key (network mode only) |
| `QDRANT_COLLECTION` | ⚠️ Optional | Auto-generated | Qdrant collection name |
| `QDRANT_INIT_MAX_ATTEMPTS` | ⚠️ Optional | `30` | Attempts for the startup Qdrant-collection init. Transient connection failures (Qdrant briefly unreachable during a rolling deploy) are retried with capped exponential backoff + jitter instead of crashlooping with a full traceback; genuine errors (auth/config, e.g. a 4xx) fail immediately. Set to `1` to restore fail-fast. |
| `QDRANT_INIT_BACKOFF_BASE` | ⚠️ Optional | `1.0` | Base delay (seconds) for the first Qdrant-init retry; subsequent retries grow exponentially (`base * 2**n`) with full jitter. |
| `QDRANT_INIT_BACKOFF_MAX` | ⚠️ Optional | `10.0` | Per-retry cap (seconds) for the Qdrant-init backoff. Size your k8s `startupProbe` accordingly (worst case ≈ `max_attempts × backoff_max` of waiting on a persistently-down Qdrant before startup finally fails). |
| `VECTOR_SYNC_SCAN_INTERVAL` | ⚠️ Optional | `300` | Document scan interval (seconds) |
| `VECTOR_SYNC_EMPTY_DISCOVERY_DELETE_THRESHOLD` | ⚠️ Optional | `3` | Fail-safe against a flaky/empty tag-discovery read. A scan deletes indexed points whose files a tag-discovery no longer returns; if a Nextcloud intermittently answers the systemtag `REPORT` with an empty result, that would wrongly purge (then re-index) the whole corpus each cycle. This is the number of **consecutive** scan cycles an index mode's discovery must return zero (while Qdrant still holds points for it) before deletions for that mode are believed — a transient empty deletes nothing; a sustained empty (a genuine mass-untag) still deletes once the streak is reached. Worst-case deletion latency for a real mass-untag ≈ `(threshold-1) × VECTOR_SYNC_SCAN_INTERVAL + 1.5 × VECTOR_SYNC_SCAN_INTERVAL`. Set `≤1` to restore immediate deletion. |
| `VECTOR_SYNC_PROCESSOR_WORKERS` | ⚠️ Optional | `3` | Concurrent indexing workers |
| `VECTOR_SYNC_FAST_CONCURRENCY` | ⚠️ Optional | unset | Per-tier override for the **fast** ingest worker's concurrency. Unset inherits `VECTOR_SYNC_PROCESSOR_WORKERS`. Must be `>= 1` when set. Resolution precedence: the worker `--concurrency` flag > this tier override > `VECTOR_SYNC_PROCESSOR_WORKERS`. |
| `VECTOR_SYNC_STRUCTURED_CONCURRENCY` | ⚠️ Optional | unset | Per-tier override for the **structured** ingest worker's concurrency. Unset inherits `VECTOR_SYNC_PROCESSOR_WORKERS`. Must be `>= 1` when set. Same precedence as `VECTOR_SYNC_FAST_CONCURRENCY`. |
| `VECTOR_SYNC_QUEUE_MAX_SIZE` | ⚠️ Optional | `10000` | Max queued documents |
| `OLLAMA_BASE_URL` | ⚠️ Optional | - | Ollama API endpoint for embeddings |
| `OLLAMA_EMBEDDING_MODEL` | ⚠️ Optional | `nomic-embed-text` | Embedding model to use |
| `OLLAMA_GENERATION_MODEL` | ⚠️ Optional | - | Ollama model for text generation |
| `OLLAMA_VERIFY_SSL` | ⚠️ Optional | `true` | Verify SSL certificates |
| `OPENAI_API_KEY` | ⚠️ Optional | - | OpenAI API key (selects OpenAI provider) |
| `OPENAI_BASE_URL` | ⚠️ Optional | - | OpenAI base URL override (for compatible APIs) |
| `OPENAI_EMBEDDING_MODEL` | ⚠️ Optional | `text-embedding-3-small` | OpenAI embedding model |
| `OPENAI_GENERATION_MODEL` | ⚠️ Optional | - | OpenAI model for text generation |
| `MISTRAL_API_KEY` | ⚠️ Optional | - | Mistral API key (selects Mistral provider) |
| `MISTRAL_EMBEDDING_MODEL` | ⚠️ Optional | `mistral-embed` | Mistral embedding model (1024-dim) |
| `MISTRAL_BASE_URL` | ⚠️ Optional | - | Mistral base URL override (proxies, on-prem) |
| `AWS_REGION` | ⚠️ Optional | - | AWS region (selects Bedrock provider) |
| `AWS_ACCESS_KEY_ID` | ⚠️ Optional | - | AWS access key (boto3 credential chain fallback) |
| `AWS_SECRET_ACCESS_KEY` | ⚠️ Optional | - | AWS secret key (boto3 credential chain fallback) |
| `BEDROCK_EMBEDDING_MODEL` | ⚠️ Optional | - | Bedrock embedding model ID |
| `BEDROCK_GENERATION_MODEL` | ⚠️ Optional | - | Bedrock generation model ID |
| `SIMPLE_EMBEDDING_DIMENSION` | ⚠️ Optional | `384` | Dimension for the fallback Simple provider |
| `DOCUMENT_CHUNK_SIZE` | ⚠️ Optional | `2048` | Characters per chunk for document embedding |
| `DOCUMENT_CHUNK_OVERLAP` | ⚠️ Optional | `200` | Overlapping characters between chunks (must be < chunk size) |
| `DOCUMENT_CHUNK_PAGE_AWARE` | ⚠️ Optional | `true` | Split PDFs on page boundaries first (one chunk per page; oversized pages split within the page). Exact page numbers, clean snippets, and a predictable ~1 chunk/page when chunk size ≥ the largest page. Set `false` for the legacy char-based path. |
| `DOCUMENT_CHUNK_PAGE_PACK` | ⚠️ Optional | `false` | Greedy page-packing (requires page-aware): merge consecutive sub-budget PDF pages into one chunk (page-range citation via `page_number`/`page_end`) instead of one-per-page. Cuts dense-vector density on lean-page/born-digital PDFs. Enabling it re-scales density fleet-wide — re-calibrate the storage rate first (Deck #636/#626). |
| `CHUNKING_CONFIG_VERSION` | ⚠️ Optional | `1` | Chunker config generation stamped on the collection sentinel. Bump on any chunker behaviour change (size, overlap, page-aware, page-pack) so the pricing density reference can't silently go stale. |
| `VECTOR_SEARCH_RRF_K` | ⚠️ Optional | `60` | Ranking constant for Reciprocal Rank Fusion in hybrid search: the fused score is `1/(rank + k)`, summed across the dense and sparse prefetches. Must be `>= 1`. Lower values make a single retriever's top hit dominate (Qdrant's own default of `2` lets a rank-0 hit from one retriever outrank a rank-3 hit both agree on); `60` is the standard value and makes cross-retriever agreement decide the ordering. Applies only when `fusion="rrf"` — DBSF has no such constant. Note the resulting scores are small (~`2/k`) and are a rank artifact, not a relevance percentage, so don't use them as an absolute `score_threshold` — use the `relevance` field and the `min_relevance` request parameter instead (ADR-034), which map every result onto a stable `[0, 1]` regardless of `k`, fusion or reranker. |
| `SEARCH_RERANK_ENABLED` | ⚠️ Optional | `false` | Enable the optional cross-encoder rerank stage. When enabled, callers may pass `rerank: true` to `POST /api/v1/search`, `POST /api/v1/vector-viz/search`, or the `nc_semantic_search` tool to have retrieved candidates re-scored against the query before being returned — generally the largest available improvement to result ordering. Requires `EMBEDDING_GATEWAY_URL` (startup fails otherwise). Off by default: it adds an upstream round-trip to every search, and what that costs depends on how your embedding gateway is deployed and provisioned — this server only knows the URL. Measure on your own deployment before enabling it broadly. Servers advertise this as `rerank_available` on `GET /api/v1/status` so clients can gate their UI instead of probing. |
| `SEARCH_RERANK_MODEL` | ⚠️ Optional | `local/BAAI/bge-reranker-v2-m3` | Rerank model, addressed the way your embedding gateway expects. **The `<provider>/` prefix is required** — the gateway splits on the FIRST slash to pick a backend, so a bare `BAAI/bge-reranker-v2-m3` resolves to a provider named `BAAI` and 503s (the search then degrades to retrieval order with `reranked: false`, which looks like "reranking does nothing" rather than an error). The prefix is also how you choose the backend: `local/` for a self-hosted cross-encoder, `bedrock/` or `openrouter/` for a hosted one. |
| `SEARCH_RERANK_POOL_SIZE` | ⚠️ Optional | `200` | Candidates handed to the reranker. Reranking can only reorder what retrieval supplied, so this depth — not the caller's `limit` — bounds how much it can improve; reranking only the rows a normal search returns captures little of the available gain. Treat as a ceiling rather than a starting point: under `granularity="document"` the grouped prefetch is bounded, and requesting more groups than it can fill makes Qdrant reorder the head of the result set before the reranker sees it. Never drops below the request's own over-fetch. Must be `>= 1`. |
| `SEARCH_RERANK_TIMEOUT_SECONDS` | ⚠️ Optional | `30.0` | Per-request rerank timeout. Generous headroom rather than a target — on expiry the search returns retrieval ordering with `reranked: false` rather than failing. Must be `> 0`. |
| `SEARCH_RERANK_MAX_CONCURRENCY` | ⚠️ Optional | `1` | Concurrent rerank calls in flight, process-wide. Bounds how many rerank requests this process keeps in flight against the gateway, so a burst of searches cannot queue unbounded work on a service shared with this server's own embedding traffic and other callers. A client-side courtesy rather than a throughput control for the gateway — raise it if yours has headroom. Must be `>= 1`. |

**Deprecated variables (still functional):**
- `VECTOR_SYNC_ENABLED` - Use `ENABLE_SEMANTIC_SEARCH` instead (will be removed in v1.0.0)

### Docker Compose Example

Enable network mode Qdrant with docker-compose:

```yaml
services:
  mcp:
    environment:
      - QDRANT_URL=http://qdrant:6333
      - ENABLE_SEMANTIC_SEARCH=true

  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - 127.0.0.1:6333:6333
    volumes:
      - qdrant-data:/qdrant/storage
    profiles:
      - qdrant  # Optional service

volumes:
  qdrant-data:
```

Start with Qdrant service:
```bash
docker-compose --profile qdrant up
```

Or use default in-memory mode (no `--profile` needed):
```bash
docker-compose up
```

---

## Decomposition Hook Points (Optional, Advanced)

The server can optionally offload embeddings to an external gateway and split
ingest into a separate scale-to-zero worker process (Deck #183). These are
**opt-in**; every default reproduces the in-process monolith behavior, so
self-hosters can ignore this section.

```bash
# Embeddings via an OpenAI-compatible gateway (else: autodetect — see above)
EMBEDDING_PROVIDER=gateway
EMBEDDING_GATEWAY_URL=https://embedding-gateway.internal
# Gateway M2M OIDC client (its own realm; leave unset to call it unauthenticated)
EMBEDDING_GATEWAY_TOKEN_URL=...
EMBEDDING_GATEWAY_CLIENT_ID=...
EMBEDDING_GATEWAY_CLIENT_SECRET=...

# Ingest queue backend. Default (unset) is "memory" — the in-process anyio
# queue — *regardless of DATABASE_URL*. procrastinate is strictly opt-in: set
# INGEST_QUEUE=postgres to split ingest into a separate worker (requires a
# PostgreSQL DATABASE_URL). A Postgres DATABASE_URL alone never enables it.
INGEST_QUEUE=postgres         # memory | postgres
# Process role (informational; the worker is launched via the `worker` command):
MCP_ROLE=all                  # api | worker | all (default)
```

### Postgres ingest queue + worker (api/worker split)

This is **opt-in**. By default (`INGEST_QUEUE=memory`) the scanner processes
changed documents in-process via anyio task groups in the API pod — no
procrastinate, no separate worker, even when `DATABASE_URL` is Postgres.

When you explicitly set `INGEST_QUEUE=postgres` (against a PostgreSQL
`DATABASE_URL`), the scanner instead **defers** one job per changed document
into the app's Postgres via
[procrastinate](https://procrastinate.readthedocs.io); a separate **worker**
process drains the queue (fetch → chunk → embed → upsert Qdrant). Run the two
roles as separate Deployments from the same image:

```bash
# API pod (always-on): serves MCP/query + runs the scanner (defers jobs)
nextcloud-mcp-server run

# Ingest worker (scale-to-zero on queue depth via KEDA): drains the queue
nextcloud-mcp-server worker -c 4
```

Notes:

- **procrastinate manages its own tables** (`procrastinate_jobs`, …) in the same
  database. They are created on a fresh DB by the API pod at startup and by
  `nextcloud-mcp-server db upgrade` — a migration lineage independent of the
  app's Alembic schema. procrastinate is Postgres-only (psycopg3); it ships in
  the `[postgres]` extra and is imported lazily.
- KEDA scales the worker on
  `SELECT count(*) FROM procrastinate_jobs WHERE queue_name='ingest' AND status='todo'`.
- `INGEST_QUEUE=postgres` with a SQLite `DATABASE_URL` is rejected at startup.
- **Teardown:** because procrastinate's schema is a separate lineage,
  `nextcloud-mcp-server db downgrade` (Alembic) does **not** drop the
  `procrastinate_*` tables. To fully revert (e.g. back to NATS or SQLite-only),
  drop them manually after downgrading:
  `DROP TABLE IF EXISTS procrastinate_jobs, procrastinate_events,
  procrastinate_periodic_defers, procrastinate_workers CASCADE;` (plus the
  `procrastinate_*` types/functions if removing the extension entirely).

---

## Capability-Gated Tools

Some tools need an upstream Nextcloud app that may be missing, disabled for the
account, or too old to serve them — Deck's card dependencies, for instance, only
exist from Deck 1.18.0. Rather than let the model discover that as a 404, the
server reads what the instance advertises on
`GET /ocs/v2.php/cloud/capabilities` and:

- **hides** the tool from `tools/list`, and
- **refuses** `tools/call` with the reason, for clients holding a stale list.

Nothing to configure — it is on by default and applies per user, so a Talk
account with Talk disabled sees no Talk tools while their colleague does.

### Behaviour

- The lookup is cached for 30 seconds per user, so enabling or upgrading an app
  makes its tools appear **without restarting the server**.
- It **fails open**: if capabilities can't be read (OCS error, unexpected
  payload, unparseable version), the tool stays listed. Availability beats
  precision — an instance we can't interrogate behaves exactly as before.
- Only apps that actually publish a capability block are gated: **Notes, Tables,
  Deck, Cookbook and Talk** (`spreed`). Calendar and Contacts tools speak
  CalDAV/CardDAV and keep working with those web apps uninstalled, so they are
  never gated; Collectives, News and Mail publish nothing to gate on.
- Version floors follow PEP 440, so a pre-release (`1.18.0-beta.3`) sorts
  *below* the release it precedes and stays gated out.

### Escape hatch

```bash
MCP_DISABLE_CAPABILITY_GATING=true    # list and run every registered tool
```

Set this if tools you expect are missing; it restores the pre-gating behaviour
so you can confirm gating is the cause (and please open an issue).

### Verifying it

```bash
docker compose exec app php occ app:disable cookbook
sleep 31                       # outlive the capability cache
# re-list tools -> nc_cookbook_* are gone; calling one returns the reason
docker compose exec app php occ app:enable cookbook
sleep 31                       # they come back, no restart
```

(Kept as a manual check rather than an automated one: the integration lane runs
tests in parallel against a shared Nextcloud, where disabling an app would race
whatever else is exercising it.)

### Adding a gate to a new tool

```python
from nextcloud_mcp_server.capabilities import require_capability

@mcp.tool()
@require_capability("deck", min_version="1.18.0")
async def deck_assign_dependent_card(ctx: Context, ...): ...
```

`app` is the OCS capability key (the app id for most apps — Talk's is `spreed`),
and `min_version` is compared against the `version` the app advertises. Whole-app
presence gates are applied automatically from `APP_CAPABILITY_KEY` in
`nextcloud_mcp_server/server/__init__.py`; only add an app there after confirming
it publishes a capability block, because a missing key is what closes the gate.

## Tag-Based File Exclusion (Optional)

Some files (contracts, medical records, credentials, private notes) should
never be exposed to an LLM, even when the assistant has valid credentials
for the account. The MCP server can hide such files from all WebDAV tools
based on **Nextcloud system tags** (the same collaborative tags users
manage from the Nextcloud UI).

### Setup

Set `EXCLUDED_TAGS` to a comma-separated list of system tag names:

```bash
EXCLUDED_TAGS=confidential,no-ai,private
```

Then create the tags in Nextcloud (one-time, as admin):

```bash
docker compose exec app php occ tag:add 'no-ai' --user-visible=true --user-assignable=false
```

`--user-assignable=false` is **strongly recommended** for the threat model
this feature is designed to address — see *Security considerations* below.
Tag any file or folder with one of these tags from the Nextcloud UI to
hide it from the MCP tools.

Empty (`EXCLUDED_TAGS=""`, the default) disables the feature entirely.

### Behaviour

When `EXCLUDED_TAGS` is set, every WebDAV MCP tool resolves the configured
tag names to file paths and applies the following:

| Tool | Effect on tagged paths |
|------|------------------------|
| `nc_webdav_list_directory` | Excluded files/folders are omitted from listings |
| `nc_webdav_read_file` | Raises `ToolError` (access denied) |
| `nc_webdav_write_file` | Raises `ToolError` (access denied) |
| `nc_webdav_create_directory` | Blocked inside excluded paths |
| `nc_webdav_delete_resource` | Raises `ToolError` (access denied) |
| `nc_webdav_move_resource` | Blocked when source **or** destination is excluded |
| `nc_webdav_copy_resource` | Blocked when source **or** destination is excluded |
| `nc_webdav_search_files` | Excluded files are filtered from results |
| `nc_webdav_find_by_name` | Excluded files are filtered from results |
| `nc_webdav_find_by_type` | Excluded files are filtered from results |
| `nc_webdav_list_favorites` | Excluded files are filtered from results |

Tagging a **folder** hides the folder itself **and** every descendant
recursively, via path-prefix match.

### Security considerations

The threat model is **preventing accidental data exfiltration via the LLM
tool surface**, not hiding files from a determined operator. Specifically:

- Create exclusion tags with `user_assignable=false` so the credentials
  the MCP server uses cannot remove the tag from a file (and thereby
  bypass the exclusion). With `user_assignable=true`, any user — including
  the one whose credentials the MCP server uses — can untag a file.
- Optionally set `user_visible=false` if the exclusion tag itself is
  sensitive metadata.
- The exclusion is enforced at the MCP tool layer only. Direct WebDAV /
  Nextcloud client access still sees the files; this feature does not
  alter Nextcloud's underlying access control.

### Performance note

The excluded path set is resolved per WebDAV tool call (1 PROPFIND for
each tag name + 1 REPORT per tag). For typical setups (a handful of
tagged files under one or two tag names) the overhead is negligible.
Caching may be added in a future release.

### Scope

This feature only covers WebDAV file operations. Notes, Calendar,
Contacts, Deck, etc. are not filtered, because they use ID-based APIs
rather than file paths.

---

## Loading Environment Variables

After creating your `.env` file, load the environment variables:

### On Linux/macOS

```bash
# Load all variables from .env
export $(grep -v '^#' .env | xargs)
```

### On Windows (PowerShell)

```powershell
# Load variables from .env
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)\s*=\s*(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
    }
}
```

### Via Docker

```bash
# Docker automatically loads .env when using --env-file
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

### Misspelled variables

Only the variables documented here are read; anything else in the environment is
ignored. To keep a typo from failing silently, startup warns when an unknown
variable closely resembles a real one:

```
WARNING  VECTOR_SYNC_ENABLE is not a recognized setting and was ignored;
         did you mean VECTOR_SYNC_ENABLED?
```

It is only a warning — an unrecognized variable never stops the server — so check
the logs after changing configuration if a setting does not seem to apply.

---

## Running Behind a Reverse Proxy

When a proxy (nginx, Traefik, Caddy, an ingress controller, Envoy) sits in front of
the server, every log line shows the *proxy's* address instead of the client's:

```
WARNING  /mcp request WITHOUT Authorization header from Address(host='172.16.0.7', port=44832)
INFO     172.16.0.7:44832 - "POST /mcp HTTP/1.1" 401
```

The server reads `X-Forwarded-For` / `X-Forwarded-Proto`, but only from proxies it
has been told to trust — the default trust list is `127.0.0.1`, so a proxy on
another host or container is ignored and its headers are discarded. Name the proxy
with `FORWARDED_ALLOW_IPS` (a comma-separated list of IP addresses and CIDR
networks, or `*` on its own to trust everything):

```bash
# Environment variable
FORWARDED_ALLOW_IPS=172.16.0.7
```

```toml
# settings.toml — equivalent, and what the Helm chart mounts as a ConfigMap
[default]
forwarded_allow_ips = "172.16.0.7"
```

The address to trust is the one the server currently logs — the proxy as seen from
the server, not the proxy's public address. Examples:

| Deployment | Value |
|------------|-------|
| docker compose, proxy in the same network | the proxy container's IP, e.g. `172.16.0.7` |
| Kubernetes, nginx-ingress or Envoy Gateway | the cluster **pod** CIDR, e.g. `10.42.0.0/16` |
| Proxy on the same host (`--net=host`, local dev) | `127.0.0.1` (already the default) |

This affects more than log readability: rate limiting on OAuth dynamic client
registration is keyed on the client address, so while the real address is unknown
every client behind the proxy shares a single bucket.

### Prefer a CIDR over `*`

`*` trusts *any* source and takes the **left-most** `X-Forwarded-For` entry, which
is attacker-controlled — a client can then claim any address it likes and evade
per-IP rate limiting. With an explicit list, the header is walked from the right
past each trusted hop, which cannot be spoofed past a proxy that appends honestly.
Use `*` only where the server is unreachable except through the proxy, and prefer a
CIDR even then.

`*` is a wildcard **only as the entire value**. Anywhere else it is inert — it
matches nothing, so `10.0.0.0/8,*` trusts the `/8` alone, narrowing the list
rather than widening it. Startup warns when a `*` is used this way.

Entries that are not valid IP addresses or networks (a hostname, or a CIDR with
host bits set such as `10.0.0.1/8`) are never matched. Startup reports the trust
list and warns about such entries:

```
INFO [2026-08-11 18:40:33] nextcloud_mcp_server.cli - Trusting X-Forwarded-* headers from: 10.0.0.1/8
WARNING [2026-08-11 18:40:33] nextcloud_mcp_server.cli - FORWARDED_ALLOW_IPS entries are not IP addresses or networks and will only ever match a client address literally: 10.0.0.1/8
```

---

## CLI Configuration

Some configuration options can also be provided via CLI arguments. CLI arguments take precedence over environment variables.

### OAuth-related CLI Options

```bash
uv run nextcloud-mcp-server --help

Options:
  --oauth / --no-oauth            Force OAuth mode (if enabled) or
                                  BasicAuth mode (if disabled). By default,
                                  auto-detected based on environment
                                  variables.
  --oauth-client-id TEXT          OAuth client ID (can also use
                                  NEXTCLOUD_OIDC_CLIENT_ID env var)
  --oauth-client-secret TEXT      OAuth client secret (can also use
                                  NEXTCLOUD_OIDC_CLIENT_SECRET env var)
  --mcp-server-url TEXT           MCP server URL for OAuth callbacks (can
                                  also use NEXTCLOUD_MCP_SERVER_URL env
                                  var)  [default: http://localhost:8000]
```

### Server Options

```bash
Options:
  -h, --host TEXT                 Server host  [default: 127.0.0.1]
  -p, --port INTEGER              Server port  [default: 8000]
  -w, --workers INTEGER           Number of worker processes
  -r, --reload                    Enable auto-reload
  -l, --log-level [critical|error|warning|info|debug|trace]
                                  Logging level  [default: info]
  -t, --transport [sse|streamable-http|http]
                                  MCP transport protocol  [default: sse]
```

### App Selection

```bash
Options:
  -e, --enable-app [notes|tables|webdav|calendar|contacts|deck]
                                  Enable specific Nextcloud app APIs. Can
                                  be specified multiple times. If not
                                  specified, all apps are enabled.
```

### Example CLI Usage

```bash
# OAuth mode with custom client and port
uv run nextcloud-mcp-server --oauth \
  --oauth-client-id abc123 \
  --oauth-client-secret xyz789 \
  --port 8080

# BasicAuth mode with specific apps only
uv run nextcloud-mcp-server --no-oauth \
  --enable-app notes \
  --enable-app calendar
```

---

## Configuration Best Practices

### For Development

- Use Single-User BasicAuth for the fastest local setup (one user, one app password)
- Store `.env` file in your project directory
- Add `.env` to `.gitignore`

### For Production

Pick the mode that matches your deployment topology — there is no single "always" answer:

- **Multi-user / hosted** — use [Login Flow v2](login-flow-v2.md). The MCP server registers with the chosen IdP (Nextcloud's built-in OIDC by default; Keycloak, AWS Cognito, etc. via `OIDC_DISCOVERY_URL`) using static `NEXTCLOUD_OIDC_CLIENT_ID` / `NEXTCLOUD_OIDC_CLIENT_SECRET` (generic OIDC creds, preferred) or RFC 7591 DCR (fallback). MCP clients authenticate via OAuth 2.1 + PKCE; per-user Nextcloud access is stored as encrypted app passwords.
- **Internal multi-user** — Multi-User BasicAuth pass-through (clients send `Authorization: Basic` headers) is fully supported when users manage their own Nextcloud credentials.
- **Personal / self-hosted** — Single-User BasicAuth with a Nextcloud app password is the simplest production setup.

In all modes:

- Use environment variables from your deployment platform (Docker secrets, Kubernetes ConfigMaps, etc.)
- Never commit credentials to version control
- SQLite *file* permissions are handled automatically by the server (it chmods
  `tokens.db` to `0600` on startup). *Directory* ownership is yours to get right
  on any host directory you bind-mount — see [For Docker](#for-docker) below.

### For Docker

Mount **one** volume for OAuth-mode deployments:

- `/app/data` — the SQLite store (`TOKEN_STORAGE_DB=/app/data/tokens.db`), holding
  the encrypted app-password store under Login Flow v2 *and* DCR-registered
  MCP-client state.

```bash
docker run \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth
```

> **Host directory ownership.** The container runs as uid 1000, so any host
> directory you bind-mount must be writable by that uid:
> `mkdir -p data && sudo chown -R 1000:0 data`. Docker-managed named volumes
> get this automatically. If you are upgrading from a release that ran as
> root, existing volumes hold root-owned files and the server will fail to
> start until they are re-owned (or recreated with `docker compose down -v`).

Use Docker secrets for sensitive values in production (`TOKEN_ENCRYPTION_KEY`, `NEXTCLOUD_OIDC_CLIENT_SECRET`, `NEXTCLOUD_PASSWORD`, etc.)

---

## See Also

- [Configuration Migration Guide v2](configuration-migration-v2.md) - **New in v0.58.0:** Migrate from old variable names
- [Authentication](authentication.md) - Authentication modes comparison
- [Login Flow v2](login-flow-v2.md) - Recommended multi-user setup
- [Running the Server](running.md) - Starting the server with different configurations
- [Troubleshooting](troubleshooting.md) - Common configuration issues
- [ADR-021](ADR-021-configuration-consolidation.md) - Configuration consolidation architecture decision
- [ADR-022](ADR-022-deployment-mode-consolidation.md) - Deployment mode consolidation

## Transport security

The MCP SDK's DNS-rebinding protection validates the `Host` and `Origin` headers.
It is **off by default**, which is what this server hardcoded before these
settings existed: MCP SDK 1.23+ auto-enables localhost-only host checking, and
that breaks Kubernetes/Docker service DNS names (see
`docs/MCP-1.23-DNS-REBINDING-FIX.md`).

| Variable | Default | Meaning |
|---|---|---|
| `MCP_DNS_REBINDING_PROTECTION` | `false` | Enable `Host`/`Origin` validation |
| `MCP_ALLOWED_HOSTS` | *(empty)* | Comma-separated `Host` allowlist. **Required** when the protection is on |
| `MCP_ALLOWED_ORIGINS` | *(empty)* | Comma-separated `Origin` allowlist. Optional |
| `CORS_ALLOW_ORIGINS` | `*` | Comma-separated CORS origin allowlist |

```bash
MCP_DNS_REBINDING_PROTECTION=true
MCP_ALLOWED_HOSTS=mcp.example.com,mcp.svc.cluster.local,localhost:*
```

A `:*` suffix allows any port (`localhost:*`).

**Enabling the protection without `MCP_ALLOWED_HOSTS` fails at startup**, on
purpose. The SDK rejects any `Host` not in the allowlist, and the default list is
empty — so the flag alone would produce a server that rejects every request,
which presents as a total outage rather than a misconfiguration. `MCP_ALLOWED_ORIGINS`
has no such requirement: `Origin` is absent on same-origin requests and the SDK
allows that case.

Setting either allowlist while the protection is off logs a warning, since the
values have no effect.

### CORS

`CORS_ALLOW_ORIGINS` defaults to `*`, unchanged from before. Note that `*`
combined with credentials is not the inert wildcard it appears to be: Starlette
echoes the request's `Origin` back rather than `*` (a bare `*` is invalid with
credentials per the CORS spec), so **any** origin may send credentialed requests.
That is fine on a private network or for local MCP Inspector use; set an explicit
list if the server is reachable from a browser. A warning is logged at startup
while the wildcard is in effect.
