# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding Conventions

### async/await Patterns
- **Use anyio + asyncio hybrid** - Both libraries are available
  - pytest runs in `anyio` mode (`anyio_mode = "auto"` in pyproject.toml)
  - asyncio used in auth modules (refresh_token_storage.py, token_exchange.py, token_broker.py)
  - anyio used in calendar.py, client_registration.py, app.py
  - Prefer standard async/await syntax without explicit library imports when possible

### Type Hints
- **Use Python 3.10+ union syntax**: `str | None` instead of `Optional[str]`
- **Use lowercase generics**: `dict[str, Any]` instead of `Dict[str, Any]`
- **Type all function signatures** - Parameters and return types
- **No explicit type checker configured** - Ruff handles linting only

### Code Quality
- **Run ruff before committing**:
  ```bash
  uv run ruff check
  uv run ruff format
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

### Project Structure
- `nextcloud_mcp_server/client/` - HTTP clients for Nextcloud APIs
- `nextcloud_mcp_server/server/` - MCP tool/resource definitions
- `nextcloud_mcp_server/auth/` - OAuth/OIDC authentication
- `nextcloud_mcp_server/models/` - Pydantic response models
- `tests/` - Layered test suite (unit, smoke, integration, load)

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

### Progressive Consent Mode (ADR-004)

**Status**: Opt-in feature (disabled by default)

**Enable**: Set `ENABLE_PROGRESSIVE_CONSENT=true`

**Default**: Hybrid Flow (backward compatible, single OAuth flow)

**What is Progressive Consent?**
- Dual OAuth flow architecture that separates client authentication (Flow 1) from resource provisioning (Flow 2)
- Flow 1: MCP client authenticates directly to IdP (aud: "mcp-server")
- Flow 2: User explicitly provisions Nextcloud access via separate login (not during MCP session)
- Provides clear separation between session tokens and background job tokens

**When to use:**
- Background jobs requiring offline access
- Enhanced security with separate authorization contexts
- Explicit user control over resource access

**When NOT to use:**
- Simple single-user deployments (use BasicAuth)
- Standard OAuth without background jobs (use default Hybrid Flow)

**Key difference from Hybrid Flow:**
- Hybrid Flow: Server intercepts OAuth callback, stores refresh token automatically
- Progressive Consent: User explicitly authorizes via `provision_nextcloud_access` tool

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

**For detailed setup, see**:
- `docs/installation.md` - Installation guide
- `docs/configuration.md` - Configuration options
- `docs/authentication.md` - Authentication modes
- `docs/running.md` - Running the server
