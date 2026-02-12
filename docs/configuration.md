# Configuration

The Nextcloud MCP server requires configuration to connect to your Nextcloud instance. Configuration is provided through environment variables, typically stored in a `.env` file.

> **Note:** Configuration was significantly simplified in v0.58.0. If you're upgrading from v0.57.x, see the [Configuration Migration Guide](configuration-migration-v2.md).

## Quick Start

We provide mode-specific configuration templates for quick setup:

```bash
# Choose a template based on your deployment mode:
cp env.sample.single-user .env         # Simplest - one user, local dev
cp env.sample.oauth-multi-user .env    # Recommended - multi-user OAuth
cp env.sample.oauth-advanced .env      # Advanced - token exchange mode

# Or start from the full example:
cp env.sample .env

# Edit .env with your Nextcloud details
```

Then choose your deployment mode:

- [Single-User BasicAuth](#single-user-basicauth-mode) - Simplest for personal instances
- [Multi-User OAuth](#multi-user-oauth-modes) - Recommended for production
- [Deployment Mode Selection](#deployment-mode-selection) - Explicit mode declaration

---

## Deployment Mode Selection

**New in v0.58.0:** You can explicitly declare your deployment mode to remove ambiguity and catch configuration errors early.

```dotenv
# Optional but recommended
MCP_DEPLOYMENT_MODE=oauth_single_audience
```

**Valid values:**
- `single_user_basic` - Single-user with username/password
- `multi_user_basic` - Multi-user with BasicAuth pass-through
- `oauth_single_audience` - Multi-user OAuth (recommended)
- `oauth_token_exchange` - Multi-user OAuth with token exchange
- `smithery` - Smithery platform deployment

**Benefits:**
- ✅ Clear which mode is active
- ✅ Better validation error messages
- ✅ Self-documenting configuration
- ✅ Catches configuration mistakes early

**Auto-detection:** If `MCP_DEPLOYMENT_MODE` is not set, the server auto-detects the mode based on other settings (existing behavior).

See [Authentication Modes](authentication.md) for detailed comparison of deployment modes.

---

## Single-User BasicAuth Mode

BasicAuth with a single user is the simplest deployment mode. Use for personal instances, local development, and testing.

```dotenv
# Minimal single-user configuration
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password

# Optional: Explicit mode declaration
MCP_DEPLOYMENT_MODE=single_user_basic
```

> [!WARNING]
> **Security Notice:** BasicAuth stores credentials in environment variables and is less secure than OAuth. Use OAuth for production multi-user deployments.

---

## Multi-User OAuth Modes

OAuth2/OIDC is the recommended authentication mode for production multi-user deployments.

### Minimal Configuration (Auto-registration)

```dotenv
# .env file for OAuth with auto-registration
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# Optional: Explicit mode declaration (recommended)
MCP_DEPLOYMENT_MODE=oauth_single_audience

# Leave these EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

This minimal configuration uses dynamic client registration to automatically register an OAuth client at startup.

### Full Configuration (Pre-configured Client)

```dotenv
# .env file for OAuth with pre-configured client
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# Optional: Explicit mode declaration (recommended)
MCP_DEPLOYMENT_MODE=oauth_single_audience

# OAuth Client Credentials (optional - auto-registers if not provided)
NEXTCLOUD_OIDC_CLIENT_ID=your-client-id
NEXTCLOUD_OIDC_CLIENT_SECRET=your-client-secret

# OAuth Callback Settings (optional)
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000

# Leave these EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEXTCLOUD_HOST` | ✅ Yes | - | Full URL of your Nextcloud instance (e.g., `https://cloud.example.com`) |
| `NEXTCLOUD_OIDC_CLIENT_ID` | ⚠️ Optional | - | OAuth client ID (auto-registers if empty) |
| `NEXTCLOUD_OIDC_CLIENT_SECRET` | ⚠️ Optional | - | OAuth client secret (auto-registers if empty) |
| `NEXTCLOUD_MCP_SERVER_URL` | ⚠️ Optional | `http://localhost:8000` | MCP server URL for OAuth callbacks |
| `NEXTCLOUD_USERNAME` | ❌ Must be empty | - | Leave empty to enable OAuth mode |
| `NEXTCLOUD_PASSWORD` | ❌ Must be empty | - | Leave empty to enable OAuth mode |

### Prerequisites

Before using OAuth configuration:

1. **Install required Nextcloud apps** (both are required):
   - **`oidc`** - OIDC Identity Provider (Apps → Security)
   - **`user_oidc`** - OpenID Connect user backend (Apps → Security)

2. **Configure the apps**:
   - Enable dynamic client registration (if using auto-registration) - Settings → OIDC
   - Enable Bearer token validation: `php occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean`

3. **Apply Bearer token patch** - The `user_oidc` app requires a patch for non-OCS endpoints - See [Upstream Status](oauth-upstream-status.md) for details

See the [OAuth Setup Guide](oauth-setup.md) for detailed step-by-step instructions, or [OAuth Quick Start](quickstart-oauth.md) for a 5-minute setup.

---

## Basic Authentication (Legacy)

Basic Authentication is maintained for backward compatibility. It uses username and password credentials.

> [!WARNING]
> **Security Notice:** Basic Authentication stores credentials in environment variables and is less secure than OAuth. Use OAuth for production deployments.

### Configuration

```dotenv
# .env file for BasicAuth mode
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
NEXTCLOUD_USERNAME=your_nextcloud_username
NEXTCLOUD_PASSWORD=your_app_password_or_password
```

### Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXTCLOUD_HOST` | ✅ Yes | Full URL of your Nextcloud instance |
| `NEXTCLOUD_USERNAME` | ✅ Yes | Your Nextcloud username |
| `NEXTCLOUD_PASSWORD` | ✅ Yes | **Recommended:** Use a dedicated [Nextcloud App Password](https://docs.nextcloud.com/server/latest/user_manual/en/session_management.html#managing-devices). Generate one in Nextcloud Security settings. Alternatively, use your login password (less secure). |

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

**Multi-User OAuth Mode:**
```dotenv
NEXTCLOUD_HOST=https://nextcloud.example.com
MCP_DEPLOYMENT_MODE=oauth_single_audience

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

> **Note:** In multi-user modes (OAuth, Multi-User BasicAuth), enabling `ENABLE_SEMANTIC_SEARCH` automatically enables background operations and refresh token storage. You don't need to set `ENABLE_BACKGROUND_OPERATIONS` separately!

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
DOCUMENT_CHUNK_SIZE=512               # Words per chunk (default: 512)
DOCUMENT_CHUNK_OVERLAP=50             # Overlapping words between chunks (default: 50)
```

> **Note:** The `VECTOR_SYNC_*` tuning parameters keep their names as they're implementation details. Only the user-facing feature flag was renamed to `ENABLE_SEMANTIC_SEARCH`.

### Embedding Service Configuration

The server uses an embedding service to generate vector representations. Two options are available:

#### Ollama (Recommended)

Use a local Ollama instance for embeddings:

```dotenv
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text  # Default model
OLLAMA_VERIFY_SSL=true                   # Verify SSL certificates
```

#### Simple Embedding Provider (Fallback)

If `OLLAMA_BASE_URL` is not set, the server uses a simple random embedding provider for testing. This is **not suitable for production** as it generates random embeddings with no semantic meaning.

### Document Chunking Configuration

The server chunks documents before embedding to handle documents larger than the embedding model's context window. Chunk size and overlap can be tuned based on your embedding model and content type.

#### Choosing Chunk Size

**Smaller chunks (256-384 words)**:
- More precise matching
- Less context per chunk
- Better for finding specific information
- Higher storage requirements (more vectors)

**Larger chunks (768-1024 words)**:
- More context per chunk
- Less precise matching
- Better for understanding broader topics
- Lower storage requirements (fewer vectors)

**Default (512 words)**:
- Balanced approach suitable for most use cases
- Works well with typical note lengths
- Good compromise between precision and context

#### Choosing Overlap

Overlap preserves context across chunk boundaries. Recommended settings:

- **10-20% of chunk size** (e.g., 50-100 words for 512-word chunks)
- **Too small** (<10%): May lose context at boundaries
- **Too large** (>20%): Redundant storage, diminishing returns

**Examples**:
```dotenv
# Precise matching for short notes
DOCUMENT_CHUNK_SIZE=256
DOCUMENT_CHUNK_OVERLAP=25

# Default balanced configuration
DOCUMENT_CHUNK_SIZE=512
DOCUMENT_CHUNK_OVERLAP=50

# More context for long documents
DOCUMENT_CHUNK_SIZE=1024
DOCUMENT_CHUNK_OVERLAP=100
```

**Important**: Changing chunk size requires re-embedding all documents. The collection naming strategy (see "Qdrant Collection Naming" above) helps manage this by creating separate collections for different configurations.

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
| `OLLAMA_VERIFY_SSL` | ⚠️ Optional | `true` | Verify SSL certificates |
| `DOCUMENT_CHUNK_SIZE` | ⚠️ Optional | `512` | Words per chunk for document embedding |
| `DOCUMENT_CHUNK_OVERLAP` | ⚠️ Optional | `50` | Overlapping words between chunks (must be < chunk size) |

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

- Use BasicAuth for quick setup and testing
- Or use OAuth with auto-registration (dynamic client registration)
- Store `.env` file in your project directory
- Add `.env` to `.gitignore`

### For Production

- **Always use OAuth2/OIDC** with pre-configured clients
- Store OAuth client credentials securely
- Use environment variables from your deployment platform (Docker secrets, Kubernetes ConfigMaps, etc.)
- Never commit credentials to version control
- SQLite database permissions are handled automatically by the server

### For Docker

- Mount OAuth client storage as a volume for persistence:
  ```bash
  docker run -v $(pwd)/.oauth:/app/.oauth --env-file .env \
    ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
  ```
- Use Docker secrets for sensitive values in production

---

## See Also

- [Configuration Migration Guide v2](configuration-migration-v2.md) - **New in v0.58.0:** Migrate from old variable names
- [OAuth Quick Start](quickstart-oauth.md) - 5-minute OAuth setup for development
- [OAuth Setup Guide](oauth-setup.md) - Detailed OAuth configuration for production
- [OAuth Architecture](oauth-architecture.md) - How OAuth works in the MCP server
- [Upstream Status](oauth-upstream-status.md) - Required patches and upstream PRs
- [Authentication](authentication.md) - Authentication modes comparison
- [Running the Server](running.md) - Starting the server with different configurations
- [Troubleshooting](troubleshooting.md) - Common configuration issues
- [OAuth Troubleshooting](oauth-troubleshooting.md) - OAuth-specific troubleshooting
- [ADR-021](ADR-021-configuration-consolidation.md) - Configuration consolidation architecture decision
