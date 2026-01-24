# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding Conventions

### async/await Patterns
- **Use anyio for all async operations** - Provides structured concurrency
  - pytest runs in `anyio` mode (`anyio_mode = "auto"` in pyproject.toml)
  - Use `anyio.create_task_group()` for concurrent execution (NOT `asyncio.gather()`)
  - Use `anyio.Lock()` for synchronization primitives (NOT `asyncio.Lock()`)
  - Use `anyio.run()` for entry points (NOT `asyncio.run()`)
  - Prefer standard async/await syntax without explicit library imports when possible
  - Examples: app.py, search/hybrid.py, search/verification.py, auth/token_broker.py

### Type Hints
- **Use Python 3.10+ union syntax**: `str | None` instead of `Optional[str]`
- **Use lowercase generics**: `dict[str, Any]` instead of `Dict[str, Any]`
- **Type all function signatures** - Parameters and return types
- **Type checker**: `ty` is configured for static type checking
  ```bash
  uv run ty check -- nextcloud_mcp_server
  ```

### Code Quality
- **Run ruff and ty before committing**:
  ```bash
  uv run ruff check
  uv run ruff format
  uv run ty check -- nextcloud_mcp_server
  ```
- **Ruff configuration** in pyproject.toml (extends select: ["I"] for import sorting)

### Error Handling
- **Use custom decorators**: `@retry_on_429` for rate limiting (see base_client.py)
- **Standard exceptions**: `HTTPStatusError` from httpx, `McpError` for MCP-specific errors
- **Logging patterns**:
  - `logger.debug()` for expected 404s and normal operations
  - `logger.warning()` for retries and non-critical issues
  - `logger.error()` for actual errors

### Testing Patterns
- **Use existing fixtures** from `tests/conftest.py` (2888 lines of test infrastructure)
- **Session-scoped fixtures** handle anyio/pytest-asyncio incompatibility
- **Mocked unit tests** use `mocker.AsyncMock(spec=httpx.AsyncClient)`
- **pytest-timeout**: 180s default per test
- **Mark tests appropriately**: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.oauth`, `@pytest.mark.smoke`

### Architectural Patterns
- **Base classes**: `BaseNextcloudClient` for all API clients
- **Pydantic responses**: All MCP tools return Pydantic models inheriting from `BaseResponse`
- **Decorators**: `@require_scopes`, `@require_provisioning` for access control
- **Context pattern**: `await get_client(ctx)` to access authenticated NextcloudClient (async!)
- **FastMCP decorators**: `@mcp.tool()`, `@mcp.resource()`
- **Token acquisition**: `get_client()` handles both pass-through and token exchange modes
  - Pass-through (default): Simple, stateless (ENABLE_TOKEN_EXCHANGE=false)
  - Token exchange (opt-in): RFC 8693 delegation (ENABLE_TOKEN_EXCHANGE=true)

### MCP Tool Annotations (ADR-017)

**All tools MUST include annotations** following these patterns:

```python
from mcp.types import ToolAnnotations

# Read-only tools (list, search, get)
@mcp.tool(
    title="Human Readable Name",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        openWorldHint=True,  # Nextcloud is external to MCP server
    ),
)

# Create operations
@mcp.tool(
    title="Create Resource",
    annotations=ToolAnnotations(
        idempotentHint=False,  # Creates new resources each time
        openWorldHint=True,
    ),
)

# Update operations (with etag/version control)
@mcp.tool(
    title="Update Resource",
    annotations=ToolAnnotations(
        idempotentHint=False,  # ETag changes = different inputs
        openWorldHint=True,
    ),
)

# Delete operations
@mcp.tool(
    title="Delete Resource",
    annotations=ToolAnnotations(
        destructiveHint=True,   # Permanently deletes data
        idempotentHint=True,    # Same end state if called repeatedly
        openWorldHint=True,
    ),
)

# HTTP PUT without version control (special case)
@mcp.tool(
    title="Write File",
    annotations=ToolAnnotations(
        idempotentHint=True,  # Same content = same end state
        openWorldHint=True,
    ),
)
```

**Key Principles**:
- **Idempotency**: Same inputs → same result. ETags change after updates, making them non-idempotent
- **Destructive**: Operations that permanently delete/overwrite data
- **Open World**: All Nextcloud tools access external service (openWorldHint=True)
- **Titles**: Use human-readable names, not snake_case function names

**See**: `docs/ADR-017-mcp-tool-annotations.md` for detailed rationale and examples

### Project Structure
- `nextcloud_mcp_server/client/` - HTTP clients for Nextcloud APIs
- `nextcloud_mcp_server/server/` - MCP tool/resource definitions
- `nextcloud_mcp_server/auth/` - OAuth/OIDC authentication
- `nextcloud_mcp_server/models/` - Pydantic response models
- `nextcloud_mcp_server/providers/` - Unified LLM provider infrastructure (embeddings + generation)
- `tests/` - Layered test suite (unit, smoke, integration, load)

### Provider Architecture (ADR-015)

**Unified Provider System** for embeddings and text generation:

**Location:** `nextcloud_mcp_server/providers/`
- `base.py` - `Provider` ABC with optional capabilities
- `registry.py` - Auto-detection and factory pattern
- `ollama.py` - Ollama provider (embeddings + generation)
- `anthropic.py` - Anthropic provider (generation only)
- `bedrock.py` - Amazon Bedrock provider (embeddings + generation)
- `simple.py` - Simple in-memory provider (embeddings only, fallback)

**Usage:**
```python
from nextcloud_mcp_server.providers import get_provider

provider = get_provider()  # Auto-detects from environment

# Check capabilities
if provider.supports_embeddings:
    embeddings = await provider.embed_batch(texts)

if provider.supports_generation:
    text = await provider.generate("prompt", max_tokens=500)
```

**Environment Variables:**

Bedrock:
- `AWS_REGION` - AWS region (e.g., "us-east-1")
- `BEDROCK_EMBEDDING_MODEL` - Embedding model ID (e.g., "amazon.titan-embed-text-v2:0")
- `BEDROCK_GENERATION_MODEL` - Generation model ID (e.g., "anthropic.claude-3-sonnet-20240229-v1:0")
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` - Optional, uses AWS credential chain

Ollama:
- `OLLAMA_BASE_URL` - API URL (e.g., "http://localhost:11434")
- `OLLAMA_EMBEDDING_MODEL` - Embedding model (default: "nomic-embed-text")
- `OLLAMA_GENERATION_MODEL` - Generation model (e.g., "llama3.2:1b")
- `OLLAMA_VERIFY_SSL` - SSL verification (default: "true")

Simple (fallback, no config needed):
- `SIMPLE_EMBEDDING_DIMENSION` - Dimension (default: 384)

**Auto-Detection Priority:** Bedrock → Ollama → Simple

**Backward Compatibility:**
- Old code using `nextcloud_mcp_server.embedding.get_embedding_service()` still works
- `EmbeddingService` now wraps `get_provider()` internally

**For Details:** See `docs/ADR-015-unified-provider-architecture.md`

## Development Commands (Quick Reference)

### Testing
```bash
# Fast feedback (recommended)
uv run pytest tests/unit/ -v                    # Unit tests (~5s)
uv run pytest -m smoke -v                       # Smoke tests (~30-60s)

# Integration tests
uv run pytest -m "integration and not oauth" -v # Without OAuth (~2-3min)
uv run pytest -m oauth -v                       # OAuth only (~3min)
uv run pytest                                   # Full suite (~4-5min)

# Coverage
uv run pytest --cov

# Specific tests after changes
uv run pytest tests/server/test_mcp.py -k "notes" -v
uv run pytest tests/client/notes/test_notes_api.py -v
```

**Important**: After code changes, rebuild the correct container:
- Single-user tests: `docker-compose up --build -d mcp`
- OAuth tests: `docker-compose up --build -d mcp-oauth`
- Keycloak tests: `docker-compose up --build -d mcp-keycloak`

### Running the Server
```bash
# Local development
export $(grep -v '^#' .env | xargs)
mcp run --transport sse nextcloud_mcp_server.app:mcp

# Docker development (rebuilds after code changes)
docker-compose up --build -d mcp        # Single-user (port 8000)
docker-compose up --build -d mcp-oauth  # Nextcloud OAuth (port 8001)
docker-compose up --build -d mcp-keycloak  # Keycloak OAuth (port 8002)
```

### Environment Setup
```bash
uv sync                # Install dependencies
uv sync --group dev    # Install with dev dependencies
```

### Load Testing
```bash
# Quick test (default: 10 workers, 30 seconds)
uv run python -m tests.load.benchmark

# Custom concurrency and duration
uv run python -m tests.load.benchmark -c 20 -d 60

# Export results for analysis
uv run python -m tests.load.benchmark --output results.json --verbose
```

**Expected Performance**: 50-200 RPS for mixed workload, p50 <100ms, p95 <500ms, p99 <1000ms.

## Database Inspection

**Credentials**: root/password, nextcloud/password, database: `nextcloud`

### Quick Query Script (Recommended for Agents)

Use `scripts/dbquery.py` for single SQL statements without requiring approval for each `docker compose exec`:

```bash
# Basic query
./scripts/dbquery.py "SELECT COUNT(*) FROM oc_users"

# Vertical output (one column per line) - useful for wide tables
./scripts/dbquery.py -E "SELECT * FROM oc_oidc_clients LIMIT 1"

# With different credentials
./scripts/dbquery.py -u nextcloud -p nextcloud "SHOW TABLES"
```

### Direct Docker Access

For interactive sessions or complex operations:

```bash
# Connect to database
docker compose exec db mariadb -u root -ppassword nextcloud

# Check OAuth clients
docker compose exec db mariadb -u root -ppassword nextcloud -e \
  "SELECT id, name, token_type FROM oc_oidc_clients ORDER BY id DESC LIMIT 10;"

# Check OAuth client scopes
docker compose exec db mariadb -u root -ppassword nextcloud -e \
  "SELECT c.id, c.name, s.scope FROM oc_oidc_clients c LEFT JOIN oc_oidc_client_scopes s ON c.id = s.client_id WHERE c.name LIKE '%MCP%';"

# Check OAuth access tokens
docker compose exec db mariadb -u root -ppassword nextcloud -e \
  "SELECT id, client_id, user_id, created_at FROM oc_oidc_access_tokens ORDER BY created_at DESC LIMIT 10;"
```

**Important Tables**:
- `oc_oidc_clients` - OAuth client registrations (DCR)
- `oc_oidc_client_scopes` - Client allowed scopes
- `oc_oidc_access_tokens` - Issued access tokens
- `oc_oidc_authorization_codes` - Authorization codes
- `oc_oidc_registration_tokens` - RFC 7592 registration tokens
- `oc_oidc_redirect_uris` - Redirect URIs

### SQLite Databases (MCP Services)

Use `scripts/sqlitequery.py` to query SQLite databases in MCP service containers:

```bash
# List tables
./scripts/sqlitequery.py ".tables"

# Query specific service
./scripts/sqlitequery.py -s oauth "SELECT * FROM refresh_tokens"
./scripts/sqlitequery.py -s keycloak "SELECT * FROM oauth_clients"
./scripts/sqlitequery.py -s basic "SELECT * FROM app_passwords"

# With column headers
./scripts/sqlitequery.py --column "SELECT * FROM audit_logs LIMIT 5"

# JSON output
./scripts/sqlitequery.py --json "SELECT * FROM oauth_sessions"

# View schema
./scripts/sqlitequery.py -s oauth ".schema refresh_tokens"
```

**Services**: `mcp` (default), `oauth`, `keycloak`, `basic`

**SQLite Tables**:
- `refresh_tokens` - OAuth refresh tokens with user profiles
- `audit_logs` - Security audit trail
- `oauth_clients` - DCR OAuth client credentials
- `oauth_sessions` - OAuth flow session state
- `registered_webhooks` - Webhook registrations
- `app_passwords` - Multi-user BasicAuth passwords
- `alembic_version` - Migration tracking

## Architecture Quick Reference

**For detailed architecture, see:**
- `docs/comparison-context-agent.md` - Overall architecture
- `docs/oauth-architecture.md` - OAuth integration patterns
- `docs/ADR-004-progressive-consent.md` - Progressive consent implementation

**Core Components**:
- `nextcloud_mcp_server/app.py` - FastMCP server entry point
- `nextcloud_mcp_server/client/` - HTTP clients (Notes, Calendar, Contacts, Tables, WebDAV)
- `nextcloud_mcp_server/server/` - MCP tool/resource definitions
- `nextcloud_mcp_server/auth/` - OAuth/OIDC authentication

**Supported Apps**: Notes, Calendar (CalDAV + VTODO tasks), Contacts (CardDAV), Tables, WebDAV, Deck, Cookbook

**Key Patterns**:
1. `NextcloudClient` orchestrates all app-specific clients
2. `BaseNextcloudClient` provides common HTTP functionality + retry logic
3. MCP tools use context pattern: `get_client(ctx)` → `NextcloudClient`
4. All operations are async using httpx

### Progressive Consent Architecture (ADR-004)

**Important**: Progressive consent is a *mechanism* for granting access, not a feature flag. The architecture is always present in OAuth mode. Whether provisioning tools are available is controlled by `ENABLE_OFFLINE_ACCESS`.

**What is Progressive Consent?**
- Dual OAuth flow architecture that separates client authentication (Flow 1) from resource provisioning (Flow 2)
- Flow 1: MCP client authenticates directly to IdP with resource scopes (notes:*, calendar:*, etc.)
  - Token audience: "mcp-server"
  - Client receives resource-scoped token for MCP session
- Flow 2: Server explicitly provisions Nextcloud access via separate login (only when `ENABLE_OFFLINE_ACCESS=true`)
  - Server requests: openid, profile, email, offline_access
  - Token audience: "nextcloud"
  - Server receives refresh token for offline access
  - Client never sees this token
- Provides clear separation between session tokens and offline access tokens

**Modes:**
- **Pass-through mode** (`ENABLE_OFFLINE_ACCESS=false`, default):
  - No Flow 2 provisioning
  - Server uses client's token to access Nextcloud (pass-through)
  - No provisioning tools available
  - Suitable for stateless, client-driven operations
- **Offline access mode** (`ENABLE_OFFLINE_ACCESS=true`):
  - Flow 2 provisioning available
  - Server stores refresh tokens for background operations
  - Provisioning tools available: `provision_nextcloud_access`, `check_logged_in`
  - Suitable for background jobs and server-initiated operations

**When to use OAuth mode:**
- Multi-user deployments
- Background jobs requiring offline access (with `ENABLE_OFFLINE_ACCESS=true`)
- Enhanced security with separate authorization contexts
- Explicit user control over resource access

**When to use BasicAuth instead:**
- Simple single-user deployments
- Local development and testing

**Key features:**
- No scope escalation - client gets exactly what it requests
- User explicitly authorizes via `provision_nextcloud_access` tool
- Clear security boundaries between MCP session and Nextcloud access

## MCP Response Patterns (CRITICAL)

**Never return raw `List[Dict]` from MCP tools** - FastMCP mangles them into dicts with numeric string keys.

**Correct Pattern**:
1. Client methods return `List[Dict]` (raw data)
2. MCP tools convert to Pydantic models and wrap in response object
3. Response models inherit from `BaseResponse`, include `results` field + metadata

**Reference implementations**:
- `nextcloud_mcp_server/models/notes.py:80` - `SearchNotesResponse`
- `nextcloud_mcp_server/models/webdav.py:113` - `SearchFilesResponse`
- `nextcloud_mcp_server/server/{notes,webdav}.py` - Tool examples

**Testing**: Extract `data["results"]` from MCP responses, not `data` directly.

## MCP Sampling for RAG (ADR-008)

**What is MCP Sampling?**
MCP sampling allows servers to request LLM completions from their clients. This enables Retrieval-Augmented Generation (RAG) patterns where the server retrieves context and the client's LLM generates answers.

**When to use sampling:**
- Generating natural language answers from retrieved documents
- Synthesizing information from multiple sources
- Creating summaries with citations

**Implementation Pattern** (see ADR-008 for details):

```python
from mcp.types import ModelHint, ModelPreferences, SamplingMessage, TextContent

@mcp.tool()
@require_scopes("notes:read")
async def nc_notes_semantic_search_answer(
    query: str, ctx: Context, limit: int = 5, max_answer_tokens: int = 500
) -> SamplingSearchResponse:
    # 1. Retrieve documents
    search_response = await nc_notes_semantic_search(query, ctx, limit)

    # 2. Check for no results (don't waste sampling call)
    if not search_response.results:
        return SamplingSearchResponse(
            query=query,
            generated_answer="No relevant documents found.",
            sources=[], total_found=0, success=True
        )

    # 3. Construct prompt with retrieved context
    prompt = f"{query}\n\nDocuments:\n{format_sources(search_response.results)}\n\nProvide answer with citations."

    # 4. Request LLM completion via sampling
    try:
        result = await ctx.session.create_message(
            messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
            max_tokens=max_answer_tokens,
            temperature=0.7,
            model_preferences=ModelPreferences(
                hints=[ModelHint(name="claude-3-5-sonnet")],
                intelligencePriority=0.8,
                speedPriority=0.5,
            ),
            include_context="thisServer",
        )

        return SamplingSearchResponse(
            query=query,
            generated_answer=result.content.text,
            sources=search_response.results,
            model_used=result.model,
            stop_reason=result.stopReason,
            success=True
        )
    except Exception as e:
        # Fallback: Return documents without generated answer
        return SamplingSearchResponse(
            query=query,
            generated_answer=f"[Sampling unavailable: {e}]\n\nFound {len(search_response.results)} documents.",
            sources=search_response.results,
            search_method="semantic_sampling_fallback",
            success=True
        )
```

**Key Points**:
- **No server-side LLM**: Server has no API keys, client controls which model is used
- **Graceful degradation**: Tool always returns useful results even if sampling fails
- **User control**: MCP clients SHOULD prompt users to approve sampling requests
- **No results optimization**: Skip sampling call when no documents found
- **Fixed prompts**: Prompts are not user-configurable to avoid injection risks

**Reference**: See `nc_notes_semantic_search_answer` in `nextcloud_mcp_server/server/notes.py:517` and ADR-008 for complete implementation.

## Testing Best Practices (MANDATORY)

### Always Run Tests
- **Run tests to completion** before considering any task complete
- **Rebuild the correct container** after code changes (see Development Commands above)
- **If tests require modifications**, ask for permission before proceeding

### Use Existing Fixtures
See `tests/conftest.py` for 2888 lines of test infrastructure:
- `nc_mcp_client` - MCP client for tool/resource testing (uses `mcp` container)
- `nc_mcp_oauth_client` - MCP client for OAuth testing (uses `mcp-oauth` container)
- `nc_client` - Direct NextcloudClient for setup/cleanup
- `temporary_note`, `temporary_addressbook`, `temporary_contact` - Auto-cleanup

### Writing Mocked Unit Tests
For client-layer response parsing tests, use mocked HTTP responses:

```python
async def test_notes_api_get_note(mocker):
    """Test that get_note correctly parses the API response."""
    mock_response = create_mock_note_response(
        note_id=123, title="Test Note", content="Test content",
        category="Test", etag="abc123"
    )

    mock_make_request = mocker.patch.object(
        NotesClient, "_make_request", return_value=mock_response
    )

    client = NotesClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    note = await client.get_note(note_id=123)

    assert note["id"] == 123
    mock_make_request.assert_called_once_with("GET", "/apps/notes/api/v1/notes/123")
```

**Mock helpers in `tests/conftest.py`**: `create_mock_response()`, `create_mock_note_response()`, `create_mock_error_response()`

**When to use**: Response parsing, error handling, request parameter building
**When NOT to use**: CalDAV/CardDAV/WebDAV protocols, OAuth flows, end-to-end MCP testing

### OAuth Testing
OAuth tests use **Playwright browser automation** to complete flows programmatically.

**Test Environment**:
- Three MCP containers: `mcp` (single-user), `mcp-oauth` (Nextcloud OIDC), `mcp-keycloak` (external IdP)
- OAuth tests require `NEXTCLOUD_HOST`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD` environment variables
- Playwright configuration: `--browser firefox --headed` for debugging
- Install browsers: `uv run playwright install firefox`

**OAuth fixtures**: `nc_oauth_client`, `nc_mcp_oauth_client`, `alice_oauth_token`, `bob_oauth_token`, etc.

**Shared OAuth Client**: All test users authenticate using a single OAuth client (created via DCR, deleted at session end via RFC 7592). Matches production behavior.

**Run OAuth tests**:
```bash
uv run pytest -m oauth -v                        # All OAuth tests
uv run pytest tests/server/oauth/ --browser firefox -v
uv run pytest tests/server/oauth/test_oauth_core.py --browser firefox --headed -v
```

### Keycloak OAuth Testing
**Validates ADR-002 architecture** for external identity providers and offline access patterns.

**Architecture**: `MCP Client → Keycloak (OAuth) → MCP Server → Nextcloud user_oidc (validates token) → APIs`

**Setup**:
```bash
docker-compose up -d keycloak app mcp-keycloak
curl http://localhost:8888/realms/nextcloud-mcp/.well-known/openid-configuration
docker compose exec app php occ user_oidc:provider keycloak
```

**Credentials**: admin/admin (Keycloak realm: `nextcloud-mcp`)

**For detailed Keycloak setup, see**:
- `docs/oauth-setup.md` - OAuth configuration
- `docs/ADR-002-vector-sync-authentication.md` - Offline access architecture
- `docs/audience-validation-setup.md` - Token audience validation
- `docs/keycloak-multi-client-validation.md` - Realm-level validation

## Integration Testing with Docker

**Nextcloud**: `docker compose exec app php occ ...` for occ commands
**MariaDB**: `docker compose exec db mariadb -u [user] -p [password] [database]` for queries

### Querying Nextcloud Application Logs

**Use this pattern** to inspect Nextcloud application logs during debugging:

```bash
# View recent log entries
docker compose exec app cat /var/www/html/data/nextcloud.log | jq | tail

# Filter by app
docker compose exec app cat /var/www/html/data/nextcloud.log | jq 'select(.app == "astrolabe")' | tail

# Filter by log level (0=DEBUG, 1=INFO, 2=WARN, 3=ERROR, 4=FATAL)
docker compose exec app cat /var/www/html/data/nextcloud.log | jq 'select(.level >= 3)' | tail

# Search for specific messages
docker compose exec app cat /var/www/html/data/nextcloud.log | jq 'select(.message | contains("OAuth"))' | tail -20

# View full exception traces
docker compose exec app cat /var/www/html/data/nextcloud.log | jq 'select(.exception != null)' | tail -5
```

**Log Structure**: Each entry is a JSON object with fields: `reqId`, `level`, `time`, `remoteAddr`, `user`, `app`, `method`, `url`, `message`, `userAgent`, `version`, `exception`

**For detailed setup, see**:
- `docs/installation.md` - Installation guide
- `docs/configuration.md` - Configuration options
- `docs/authentication.md` - Authentication modes
- `docs/running.md` - Running the server

**For additional information regarding MCP during development, see**:
- `../../Software/modelcontextprotocol/` - MCP spec
- `../../Software/python-sdk/` - Python MCP SDK
