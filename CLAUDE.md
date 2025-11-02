# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Testing

The test suite is organized in layers for fast feedback:

```bash
# FAST FEEDBACK (recommended for development)
# Unit tests only - ~5 seconds
uv run pytest tests/unit/ -v

# Smoke tests - critical path validation - ~30-60 seconds
uv run pytest -m smoke -v

# INTEGRATION TESTS
# Integration tests without OAuth - ~2-3 minutes
uv run pytest -m "integration and not oauth" -v

# Full test suite - ~4-5 minutes
uv run pytest

# OAuth tests only (slowest, requires Playwright) - ~3 minutes
uv run pytest -m oauth -v

# COVERAGE
# Run tests with coverage
uv run pytest --cov

# LEGACY COMMANDS (still work)
# Run all integration tests
uv run pytest -m integration -v

# Skip integration tests
uv run pytest -m "not integration" -v
```

! Hint: If the tests are failing due to missing environment variables, then usually the correct .env has not been created or not correctly configured yet.

### Load Testing
```bash
# Run benchmark with default settings (10 workers, 30 seconds)
uv run python -m tests.load.benchmark

# Quick test with custom concurrency and duration
uv run python -m tests.load.benchmark --concurrency 20 --duration 60

# Extended load test (50 workers for 5 minutes)
uv run python -m tests.load.benchmark -c 50 -d 300

# Export results to JSON for analysis
uv run python -m tests.load.benchmark -c 20 -d 60 --output results.json

# Test OAuth server on port 8001
uv run python -m tests.load.benchmark --url http://127.0.0.1:8001/mcp

# Verbose mode with detailed logging
uv run python -m tests.load.benchmark -c 10 -d 30 --verbose
```

**Load Testing Features:**
- **Mixed workload** simulating realistic MCP usage (40% reads, 20% writes, 15% search, 25% other operations)
- **Real-time progress** bar with live RPS and error counts
- **Detailed metrics**:
  - Throughput (requests/second)
  - Latency percentiles (p50, p90, p95, p99)
  - Per-operation breakdown
  - Error rates and types
- **Automatic cleanup** of test data
- **JSON export** for CI/CD integration
- **Server health checks** before starting

**Understanding Results:**
- **Requests/Second (RPS)**: Higher is better. Expected baseline: 50-200 RPS for mixed workload
- **Latency**:
  - p50 (median): Should be <100ms for most operations
  - p95: Should be <500ms
  - p99: Should be <1000ms
- **Error Rate**: Should be <1% under normal load

**Common Bottlenecks:**
1. Nextcloud backend API response times (most common)
2. Database connection limits
3. HTTP client connection pooling
4. Network I/O between containers

### Code Quality
```bash
# Format and lint code
uv run ruff check
uv run ruff format

# Type checking
# No explicit type checker configured - this is a Python project using ruff for linting
```

### Running the Server
```bash
# Local development - load environment variables and run
export $(grep -v '^#' .env | xargs)
mcp run --transport sse nextcloud_mcp_server.app:mcp

# Docker development environment with Nextcloud instance
docker-compose up

# After code changes, rebuild and restart the appropriate MCP server container:
# For basic auth changes (most common) - uses admin credentials
docker-compose up --build -d mcp

# For OAuth changes - uses OAuth authentication with JWT tokens
docker-compose up --build -d mcp-oauth

# Build Docker image
docker build -t nextcloud-mcp-server .
```

**Important: MCP Server Containers**
- **`mcp`** (port 8000): Uses basic auth with admin credentials. Use this for most development and testing.
- **`mcp-oauth`** (port 8001): Uses OAuth authentication with JWT tokens. Use this when working on OAuth-specific features or tests.
  - JWT tokens are used for testing (faster validation, scopes embedded in token)
  - The server can handle both JWT and opaque tokens via the token verifier

### Environment Setup
```bash
# Install dependencies
uv sync

# Install development dependencies
uv sync --group dev
```

### Database Inspection

**Docker Compose Database Credentials:**
- Root user: `root` / password: `password`
- App user: `nextcloud` / password: `password`
- Database: `nextcloud`

**Common Database Commands:**
```bash
# Connect to database as root (most common for inspection)
docker compose exec db mariadb -u root -ppassword nextcloud

# Check OAuth clients
docker compose exec db mariadb -u root -ppassword nextcloud -e "SELECT id, name, token_type FROM oc_oidc_clients ORDER BY id DESC LIMIT 10;"

# Check OAuth client scopes
docker compose exec db mariadb -u root -ppassword nextcloud -e "SELECT c.id, c.name, s.scope FROM oc_oidc_clients c LEFT JOIN oc_oidc_client_scopes s ON c.id = s.client_id WHERE c.name LIKE '%MCP%';"

# Check OAuth access tokens
docker compose exec db mariadb -u root -ppassword nextcloud -e "SELECT id, client_id, user_id, created_at FROM oc_oidc_access_tokens ORDER BY created_at DESC LIMIT 10;"
```

**Important Tables:**
- `oc_oidc_clients` - OAuth client registrations (DCR clients)
- `oc_oidc_client_scopes` - Client allowed scopes
- `oc_oidc_access_tokens` - Issued access tokens
- `oc_oidc_authorization_codes` - Authorization codes
- `oc_oidc_registration_tokens` - RFC 7592 registration tokens for client management
- `oc_oidc_redirect_uris` - Redirect URIs for each client

## Architecture Overview

This is a Python MCP (Model Context Protocol) server that provides LLM integration with Nextcloud. The architecture follows a layered pattern:

### Core Components

- **`nextcloud_mcp_server/app.py`** - Main MCP server entry point using FastMCP framework
- **`nextcloud_mcp_server/client/`** - HTTP client implementations for different Nextcloud APIs
- **`nextcloud_mcp_server/server/`** - MCP tool/resource definitions that expose client functionality
- **`nextcloud_mcp_server/controllers/`** - Business logic controllers (e.g., notes search)

### Client Architecture

- **`NextcloudClient`** - Main orchestrating client that manages all app-specific clients
- **`BaseNextcloudClient`** - Abstract base class providing common HTTP functionality and retry logic
- **App-specific clients**: `NotesClient`, `CalendarClient`, `ContactsClient`, `TablesClient`, `WebDAVClient`

### Server Integration

Each Nextcloud app has a corresponding server module that:
1. Defines MCP tools using `@mcp.tool()` decorators
2. Defines MCP resources using `@mcp.resource()` decorators
3. Uses the context pattern to access the `NextcloudClient` instance

### Supported Nextcloud Apps

- **Notes** - Full CRUD operations and search
- **Calendar** - CalDAV integration with events, recurring events, attendees, and **tasks (VTODO)**
  - **Calendar Operations**: List, create, delete calendars
  - **Event Operations**: Full CRUD, recurring events, attendees, reminders, bulk operations
  - **Task Operations (VTODO)**: Full CRUD for CalDAV tasks with:
    - Status tracking (NEEDS-ACTION, IN-PROCESS, COMPLETED, CANCELLED)
    - Priority levels (0-9, 1=highest, 9=lowest)
    - Due dates, start dates, completion tracking
    - Percent complete (0-100%)
    - Categories and filtering
    - Search across all calendars
  - **Note**: Calendar implementation uses caldav library's AsyncDavClient
- **Contacts** - CardDAV integration with address book operations
- **Tables** - Row-level operations on Nextcloud Tables
- **WebDAV** - Complete file system access

### Key Patterns

1. **Environment-based configuration** - Uses `NextcloudClient.from_env()` to load credentials from environment variables
2. **Async/await throughout** - All operations are async using httpx
3. **Retry logic** - `@retry_on_429` decorator handles rate limiting
4. **Context injection** - MCP context provides access to the authenticated client instance
5. **Modular design** - Each Nextcloud app is isolated in its own client/server pair

### MCP Response Patterns

**CRITICAL: Never return raw `List[Dict]` from MCP tools - always wrap in Pydantic response models**

FastMCP serialization issue: raw lists get mangled into dicts with numeric string keys.

**Pattern:**
1. Client methods return `List[Dict]` (raw data)
2. MCP tools convert to Pydantic models and wrap in response object
3. Response models inherit from `BaseResponse`, include `results` field + metadata

**Reference implementations:**
- `SearchNotesResponse` in `nextcloud_mcp_server/models/notes.py:80`
- `SearchFilesResponse` in `nextcloud_mcp_server/models/webdav.py:113`
- Tool examples: `nextcloud_mcp_server/server/{notes,webdav}.py`

**Testing:** Extract `data["results"]` from MCP responses, not `data` directly.

### Testing Structure

The test suite follows a layered architecture for fast feedback:

```
tests/
├── unit/                    # Fast unit tests (~5s total)
│   ├── test_scope_decorator.py
│   └── test_response_models.py
├── smoke/                   # Critical path tests (~30-60s)
│   └── test_smoke.py
├── integration/
│   ├── client/             # Direct API layer tests
│   │   ├── notes/
│   │   ├── calendar/
│   │   └── ...
│   └── server/             # MCP tool layer tests
│       ├── oauth/          # OAuth-specific tests (slow, ~3min)
│       │   ├── test_oauth_core.py
│       │   ├── test_scope_authorization.py
│       │   └── ...
│       ├── test_mcp.py
│       └── ...
└── load/                   # Performance tests
```

**Test Markers:**
- `@pytest.mark.unit` - Fast unit tests with mocked dependencies
- `@pytest.mark.integration` - Integration tests requiring Docker containers
- `@pytest.mark.oauth` - OAuth tests requiring Playwright (slowest)
- `@pytest.mark.smoke` - Critical path smoke tests

**Fixtures** in `tests/conftest.py` - Shared test setup and utilities
- **Important**: Integration tests run against live Docker containers. After making code changes:
  - For basic auth tests: rebuild with `docker-compose up --build -d mcp`
  - For OAuth tests: rebuild with `docker-compose up --build -d mcp-oauth`

#### Testing Best Practices
- **MANDATORY: Always run tests after implementing features or fixing bugs**
  - Run tests to completion before considering any task complete
  - If tests require modifications to pass, ask for permission before proceeding
  - **Rebuild the correct container** after code changes:
    - For basic auth tests (most common): `docker-compose up --build -d mcp`
    - For OAuth tests: `docker-compose up --build -d mcp-oauth`
- **Use existing fixtures** from `tests/conftest.py` to avoid duplicate setup work:
  - `nc_mcp_client` - MCP client session for tool/resource testing (uses `mcp` container)
  - `nc_mcp_oauth_client` - MCP client session for OAuth testing (uses `mcp-oauth` container)
  - `nc_client` - Direct NextcloudClient for setup/cleanup operations
  - `temporary_note` - Creates and cleans up test notes automatically
  - `temporary_addressbook` - Creates and cleans up test address books
  - `temporary_contact` - Creates and cleans up test contacts
- **Test specific functionality** after changes:
  - For Notes changes: `uv run pytest tests/server/test_mcp.py -k "notes" -v`
  - For specific API changes: `uv run pytest tests/client/notes/test_notes_api.py -v`
  - For OAuth changes: `uv run pytest tests/server/test_oauth*.py -v` (remember to rebuild `mcp-oauth` container)
- **Avoid creating standalone test scripts** - use pytest with proper fixtures instead

#### Writing Mocked Unit Tests

For client-layer tests that verify response parsing logic, use mocked HTTP responses instead of real network calls:

**Pattern:**
```python
import httpx
import pytest
from nextcloud_mcp_server.client.notes import NotesClient
from tests.conftest import create_mock_note_response

async def test_notes_api_get_note(mocker):
    """Test that get_note correctly parses the API response."""
    # Create mock response using helper functions
    mock_response = create_mock_note_response(
        note_id=123,
        title="Test Note",
        content="Test content",
        category="Test",
        etag="abc123",
    )

    # Mock the _make_request method
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NotesClient, "_make_request", return_value=mock_response
    )

    # Create client and test
    client = NotesClient(mock_client, "testuser")
    note = await client.get_note(note_id=123)

    # Verify the response was parsed correctly
    assert note["id"] == 123
    assert note["title"] == "Test Note"
    # Verify the correct API endpoint was called
    mock_make_request.assert_called_once_with("GET", "/apps/notes/api/v1/notes/123")
```

**Mock Response Helpers in `tests/conftest.py`:**
- `create_mock_response()` - Generic HTTP response builder
- `create_mock_note_response()` - Pre-configured note response
- `create_mock_error_response()` - Error responses (404, 412, etc.)

**Benefits:**
- ⚡ Fast execution (~0.1s vs minutes for integration tests)
- 🔒 No Docker dependency
- 🎯 Tests focus on response parsing logic
- ♻️ Repeatable and deterministic

**When to use:**
- Testing client methods that parse JSON responses
- Testing error handling (404, 412, etc.)
- Testing request parameter building

**When NOT to use (keep as integration tests):**
- Complex protocol interactions (CalDAV, CardDAV, WebDAV)
- Multi-component workflows (Notes + WebDAV attachments)
- OAuth flows
- End-to-end MCP tool testing

**Reference Implementation:**
- See `tests/client/notes/test_notes_api.py` for complete examples
- Mark unit tests with `pytestmark = pytest.mark.unit`
- Run with: `uv run pytest tests/unit/ tests/client/notes/test_notes_api.py -v`

#### OAuth/OIDC Testing
OAuth integration tests use **automated Playwright browser automation** to complete the OAuth flow programmatically.

**OAuth Testing Setup:**
- **Main fixtures**: `nc_oauth_client`, `nc_mcp_oauth_client` - Use Playwright automation
- **Shared OAuth Client**: All test users authenticate using a single OAuth client
  - **Created fresh for each test session** via Dynamic Client Registration (DCR)
  - Matches production MCP server behavior (one client, multiple user tokens)
  - Each user gets their own unique access token
  - **Automatic cleanup**: Client is registered at session start, deleted at session end (RFC 7592)
  - Implementation: `shared_oauth_client_credentials` fixture in `tests/conftest.py`
  - **Note**: Client deletion may fail due to Nextcloud middleware (logged as warning). This doesn't affect tests.
- **Available fixtures**: `playwright_oauth_token`, `nc_oauth_client`, `nc_mcp_oauth_client`
- **Multi-user fixtures**: `alice_oauth_token`, `bob_oauth_token`, `charlie_oauth_token`, `diana_oauth_token`
- **Requirements**: `NEXTCLOUD_HOST`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD` environment variables
- Uses `pytest-playwright-asyncio` for async Playwright fixtures
- **Playwright configuration**: Use pytest CLI args like `--browser firefox --headed` to customize
- **Install browsers**: `uv run playwright install firefox` (or `chromium`, `webkit`)

**Example Commands:**
```bash
# Run all OAuth tests with Playwright automation using Firefox
uv run pytest tests/server/oauth/ --browser firefox -v

# Run specific OAuth test file with visible browser for debugging
uv run pytest tests/server/oauth/test_oauth_core.py --browser firefox --headed -v

# Run with Chromium (default) - use -m oauth marker for all OAuth tests
uv run pytest -m oauth -v
```

**Test Environment:**
- **Two MCP server containers are available:**
  - `mcp` (port 8000): Uses basic auth with admin credentials - for most testing
  - `mcp-oauth` (port 8001): Uses OAuth authentication - for OAuth-specific testing
- Start OAuth MCP server: `docker-compose up --build -d mcp-oauth`
- **Important**: When working on OAuth functionality, always rebuild `mcp-oauth` container, not `mcp`

**CI/CD Notes:**
- Playwright tests run in CI/CD environments
- Use Firefox browser in CI: `--browser firefox` (Chromium may have issues with localhost redirects)

#### Keycloak OAuth/OIDC Testing (ADR-002 Integration)

The MCP server supports using **Keycloak as an external OAuth/OIDC identity provider** instead of Nextcloud's built-in OIDC app. This validates the ADR-002 architecture for background jobs and external identity providers.

**Architecture:**
```
MCP Client → Keycloak (OAuth) → MCP Server → Nextcloud user_oidc (validates token) → APIs
```

**Key Benefits:**
- ✅ **No admin credentials needed** - All API access uses user's Keycloak token
- ✅ **External identity provider** - Demonstrates integration with enterprise IdPs
- ✅ **ADR-002 validation** - Tests offline_access and refresh token patterns
- ✅ **User provisioning** - Nextcloud automatically provisions users from Keycloak

**Setup and Testing:**
```bash
# 1. Start Keycloak and MCP server with Keycloak OAuth
docker-compose up -d keycloak app mcp-keycloak

# 2. Verify Keycloak realm is available
curl http://localhost:8888/realms/nextcloud-mcp/.well-known/openid-configuration

# 3. Verify user_oidc provider is configured
docker compose exec app php occ user_oidc:provider keycloak

# 4. Generate encryption key for refresh token storage (optional, for offline access)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Set in environment: export TOKEN_ENCRYPTION_KEY='<key>'

# 5. Test OAuth flow manually
# Get token from Keycloak:
TOKEN=$(curl -s -X POST "http://localhost:8888/realms/nextcloud-mcp/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=mcp-client" \
  -d "client_secret=mcp-secret-change-in-production" \
  -d "username=admin" \
  -d "password=admin" \
  -d "scope=openid profile email offline_access" | jq -r .access_token)

# Use token with Nextcloud API (validated by user_oidc):
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/ocs/v2.php/cloud/capabilities

# 6. Connect MCP client
# Point client to: http://localhost:8002
# Complete OAuth flow using Keycloak credentials: admin/admin
```

**Three MCP Server Containers:**
- **`mcp`** (port 8000): Basic auth with admin credentials
- **`mcp-oauth`** (port 8001): Nextcloud OIDC provider (JWT tokens)
- **`mcp-keycloak`** (port 8002): Keycloak OIDC provider (external IdP)

**Keycloak Configuration:**
- **Realm**: `nextcloud-mcp` (auto-imported from `keycloak/realm-export.json`)
- **Client**: `mcp-client` (pre-configured with PKCE, offline_access)
- **Admin user**: `admin/admin` (created in realm export)
- **Redirect URIs**: `http://localhost:*/callback`, `http://127.0.0.1:*/callback`

**Environment Variables** (Generic OIDC - works with any provider):
```bash
# Generic OIDC configuration (provider-agnostic)
OIDC_DISCOVERY_URL=http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration
OIDC_CLIENT_ID=nextcloud-mcp-server        # OAuth client ID
OIDC_CLIENT_SECRET=mcp-secret-...          # OAuth client secret

# Nextcloud API configuration
NEXTCLOUD_HOST=http://app:80               # Nextcloud API (token validation in external IdP mode)

# Refresh tokens and token exchange (ADR-002)
ENABLE_OFFLINE_ACCESS=true                 # Enable refresh tokens
TOKEN_ENCRYPTION_KEY=<fernet-key>          # Encrypt refresh tokens
TOKEN_STORAGE_DB=/app/data/tokens.db       # Token storage path

# OAuth scopes (optional - uses defaults if not specified)
NEXTCLOUD_OIDC_SCOPES=openid profile email offline_access notes:read notes:write ...
```

**Provider Mode Detection:**
- **External IdP mode**: If `OIDC_DISCOVERY_URL` issuer ≠ `NEXTCLOUD_HOST` → Uses external provider (Keycloak, Auth0, Okta, etc.)
- **Integrated mode**: If `OIDC_DISCOVERY_URL` not set or issuer = `NEXTCLOUD_HOST` → Uses Nextcloud OIDC app

**Nextcloud user_oidc Configuration:**
The `user_oidc` app is automatically configured by `app-hooks/post-installation/15-setup-keycloak-provider.sh`:
```bash
# Configured with:
--check-bearer=1          # Validate bearer tokens
--bearer-provisioning=1   # Auto-provision users
--unique-uid=1            # Hash user IDs
--scope="openid profile email offline_access"
```

**Troubleshooting:**
```bash
# Check Keycloak is running
docker-compose ps keycloak
docker-compose logs keycloak

# Check user_oidc provider configuration
docker compose exec app php occ user_oidc:provider keycloak

# Check MCP server logs
docker-compose logs -f mcp-keycloak

# Check Nextcloud logs for token validation
docker compose exec app tail -f /var/www/html/data/nextcloud.log

# Verify Keycloak is accessible from Nextcloud container
docker compose exec app curl http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration
```

**ADR-002 Offline Access Testing:**
The Keycloak integration enables testing ADR-002's primary authentication pattern (offline access with refresh tokens):

1. **Refresh token storage**: Tokens stored encrypted in SQLite (`/app/data/tokens.db`)
2. **Token refresh**: Access tokens refreshed automatically when expired
3. **Background workers**: Can access APIs using stored refresh tokens
4. **No admin credentials**: All operations use user's OAuth tokens

**Note**: Service account tokens (client_credentials grant) were considered but rejected as they create Nextcloud user accounts and violate OAuth "act on-behalf-of" principles. See ADR-002 "Will Not Implement" section.

See `docs/ADR-002-vector-sync-authentication.md` for architectural details.

**Audience Validation:**
Tokens include `aud: ["mcp-server", "nextcloud"]` claims for proper security:
- MCP server validates tokens are intended for it
- Nextcloud validates tokens include it as audience
- Prevents token misuse across services

See `docs/audience-validation-setup.md` for configuration details and `docs/keycloak-multi-client-validation.md` for realm-level validation behavior.

### Configuration Files

- **`pyproject.toml`** - Python project configuration using uv for dependency management
- **`.env`** (from `env.sample`) - Environment variables for Nextcloud connection
- **`docker-compose.yml`** - Complete development environment with Nextcloud + database

## Integration testing with docker

### Nextcloud

- The `app` container is running nextcloud.
- Use `docker compose exec app php occ ...` to get a list of available commands

### Mariadb

- The `db` container is running mariadb
- Use `docker compose exec db mariadb -u [user] -p [password] [database]` to execute queries. Check the docker-compose file for credentials
