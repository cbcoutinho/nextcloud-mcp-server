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

> **Note:** The legacy templates `env.sample.oauth-multi-user` and `env.sample.oauth-advanced` configure the deprecated direct-OAuth-to-Nextcloud modes. New deployments should use [Login Flow v2](login-flow-v2.md) for multi-user setups.

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
| `NEXTCLOUD_PUBLIC_ISSUER_URL` | ✅ Yes | Public URL of Nextcloud (for browser redirects during Login Flow v2) |
| `NEXTCLOUD_OIDC_CLIENT_ID` | ⚠️ Optional (preferred) | OIDC client ID for the MCP server's relying-party registration with the IdP (Nextcloud OIDC by default; Keycloak / Cognito / etc. via `OIDC_DISCOVERY_URL`). If unset and the IdP advertises a `registration_endpoint`, RFC 7591 DCR is used as fallback. |
| `NEXTCLOUD_OIDC_CLIENT_SECRET` | ⚠️ Optional (preferred) | OIDC client secret paired with `NEXTCLOUD_OIDC_CLIENT_ID`. |
| `OIDC_DISCOVERY_URL` | Optional | Override the IdP discovery URL. Defaults to `${NEXTCLOUD_HOST}/.well-known/openid-configuration` (Nextcloud's built-in OIDC). Set to a Keycloak realm or AWS Cognito user-pool discovery URL to use an external IdP. |

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
DATABASE_URL=postgresql+asyncpg://mcp:secret@postgres.svc.cluster.local:5432/mcp
TOKEN_ENCRYPTION_KEY=<fernet-key>
```

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Optional | SQLAlchemy async URL for any supported backend. When set, wins over `TOKEN_STORAGE_DB`. Primary supported targets: `postgresql+asyncpg://...` (recommended for HA) and `sqlite+aiosqlite:///...` (development). |
| `TOKEN_STORAGE_DB` | Optional | Legacy SQLite-only path. Used when `DATABASE_URL` is unset. Falls back to a per-process ephemeral tempfile when both are unset. |
| `DATABASE_VERIFY_SSL` | Optional | TLS verification toggle for the Postgres backend. Unset (default) → asyncpg's `prefer` mode (TLS if offered, no verification — keeps cluster-internal Postgres working). `true` → full cert verification. `false` → silence cert errors (homelab / self-signed). |
| `DATABASE_CA_BUNDLE` | Optional | Path to a PEM file containing a private CA. Implies `DATABASE_VERIFY_SSL=true`. Use this for self-hosted Postgres signed by your homelab CA instead of disabling verification. |
| `DATABASE_POOL_SIZE` | Deprecated, no-op | Was per-pod SQLAlchemy pool size for the Postgres backend. The engine now uses `NullPool` (one fresh asyncpg connection per checkout) to avoid cross-event-loop crashes under anyio TaskGroups — see [ADR-026 § Connection pool](ADR-026-pluggable-database-backend.md) and [#799](https://github.com/cbcoutinho/nextcloud-mcp-server/pull/799). Still accepted for backward compatibility; setting it has no effect. |
| `DATABASE_MAX_OVERFLOW` | Deprecated, no-op | Was per-pod burst connection cap on top of `DATABASE_POOL_SIZE`. Now ignored (see above). |

The asyncpg engine is `NullPool`-only: each `engine.connect()` opens
and tears down a fresh asyncpg connection in the caller's current
event loop. On LAN-local Postgres the per-connection overhead is a
single round-trip (~5 ms), so the throughput cost is negligible for
the MCP server's traffic shape (low concurrency, bursty per-user
requests).

Homelab example (self-signed Postgres with a private CA):

```env
DATABASE_URL=postgresql+asyncpg://mcp:secret@pg.lan:5432/mcp
DATABASE_CA_BUNDLE=/etc/ssl/certs/homelab-ca.pem
TOKEN_ENCRYPTION_KEY=<fernet-key>
```

Notes:

- **PyPI extra required.** The `asyncpg` driver is an optional extra so
  the default `pip install nextcloud-mcp-server` stays lean. Install
  with `pip install 'nextcloud-mcp-server[postgres]'` when using a
  Postgres URL. The Docker image bundles it by default. When
  `DATABASE_URL=postgresql+asyncpg://...` is set without the extra,
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
  `DATABASE_URL=postgresql+asyncpg://mcp:mcp@localhost:5433/mcp`.

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
VECTOR_SYNC_QUEUE_MAX_SIZE=10000      # Max queued documents (default: 10000)

# Document chunking settings (for vector embeddings)
DOCUMENT_CHUNK_SIZE=2048              # Characters per chunk (default: 2048)
DOCUMENT_CHUNK_OVERLAP=200            # Overlapping characters between chunks (default: 200)
```

> **Note:** The `VECTOR_SYNC_*` tuning parameters keep their names as they're implementation details. Only the user-facing feature flag was renamed to `ENABLE_SEMANTIC_SEARCH`.

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
  search; configure `VECTOR_SYNC_PDF_TAG` to change the tag name. The `file`
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
| `QDRANT_URL` | ⚠️ Optional | - | Qdrant service URL (network mode) - mutually exclusive with `QDRANT_LOCATION` |
| `QDRANT_LOCATION` | ⚠️ Optional | `:memory:` | Local Qdrant path (`:memory:` or `/path/to/data`) - mutually exclusive with `QDRANT_URL` |
| `QDRANT_API_KEY` | ⚠️ Optional | - | Qdrant API key (network mode only) |
| `QDRANT_COLLECTION` | ⚠️ Optional | Auto-generated | Qdrant collection name |
| `VECTOR_SYNC_SCAN_INTERVAL` | ⚠️ Optional | `300` | Document scan interval (seconds) |
| `VECTOR_SYNC_PROCESSOR_WORKERS` | ⚠️ Optional | `3` | Concurrent indexing workers |
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
TENANT_ID=<uuid>              # per-tenant identity (used in collection naming)
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
- SQLite database permissions are handled automatically by the server

### For Docker

Mount **two** volumes for OAuth-mode deployments:

- `/app/.oauth` — DCR-registered MCP-client state (only used when DCR is the chosen registration path; harmless to mount otherwise).
- `/app/data` — encrypted app-password store under Login Flow v2 (`TOKEN_STORAGE_DB=/app/data/tokens.db`).

```bash
docker run \
  -v $(pwd)/.oauth:/app/.oauth \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth
```

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
