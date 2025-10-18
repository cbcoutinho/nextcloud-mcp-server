# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Testing
```bash
# Run all tests
uv run pytest

# Run integration tests only
uv run pytest -m integration

# Run tests with coverage
uv run pytest --cov

# Skip integration tests
uv run pytest -m "not integration"
```

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

# For OAuth changes - uses OAuth authentication flow
docker-compose up --build -d mcp-oauth

# Build Docker image
docker build -t nextcloud-mcp-server .
```

**Important: Two MCP Server Containers**
- **`mcp`** (port 8000): Uses basic auth with admin credentials. Use this for most development and testing.
- **`mcp-oauth`** (port 8001): Uses OAuth authentication. Only use this when working on OAuth-specific features or tests.

### Environment Setup
```bash
# Install dependencies
uv sync

# Install development dependencies
uv sync --group dev
```

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
- **Calendar** - CalDAV integration with events, recurring events, attendees
- **Contacts** - CardDAV integration with address book operations
- **Tables** - Row-level operations on Nextcloud Tables
- **WebDAV** - Complete file system access

### Key Patterns

1. **Environment-based configuration** - Uses `NextcloudClient.from_env()` to load credentials from environment variables
2. **Async/await throughout** - All operations are async using httpx
3. **Retry logic** - `@retry_on_429` decorator handles rate limiting
4. **Context injection** - MCP context provides access to the authenticated client instance
5. **Modular design** - Each Nextcloud app is isolated in its own client/server pair

### Testing Structure

- **Integration tests** in `tests/client/` and `tests/server/` - Test real Nextcloud API interactions
- **Fixtures** in `tests/conftest.py` - Shared test setup and utilities
- Tests are marked with `@pytest.mark.integration` for selective running
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

#### OAuth/OIDC Testing
OAuth integration tests use **automated Playwright browser automation** to complete the OAuth flow programmatically.

**OAuth Testing Setup:**
- **Main fixtures**: `nc_oauth_client`, `nc_mcp_oauth_client` - Use Playwright automation
- **Shared OAuth Client**: All test users authenticate using a single OAuth client
  - Stored in `.nextcloud_oauth_shared_test_client.json`
  - Matches production MCP server behavior
  - Each user gets their own unique access token
  - Implementation: `shared_oauth_client_credentials` fixture in `tests/conftest.py`
- **Available fixtures**: `playwright_oauth_token`, `nc_oauth_client`, `nc_mcp_oauth_client`
- **Multi-user fixtures**: `alice_oauth_token`, `bob_oauth_token`, `charlie_oauth_token`, `diana_oauth_token`
- **Requirements**: `NEXTCLOUD_HOST`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD` environment variables
- Uses `pytest-playwright-asyncio` for async Playwright fixtures
- **Playwright configuration**: Use pytest CLI args like `--browser firefox --headed` to customize
- **Install browsers**: `uv run playwright install firefox` (or `chromium`, `webkit`)

**Example Commands:**
```bash
# Run all OAuth tests with Playwright automation using Firefox
uv run pytest tests/server/test_oauth*.py --browser firefox -v

# Run specific tests with visible browser for debugging
uv run pytest tests/server/test_mcp_oauth.py --browser firefox --headed -v

# Run with Chromium (default)
uv run pytest tests/server/test_oauth*.py -v
```

**Test Environment:**
- **Two MCP server containers are available:**
  - `mcp` (port 8000): Uses basic auth with admin credentials - for most testing
  - `mcp-oauth` (port 8001): Uses OAuth authentication - for OAuth-specific testing
- Start OAuth MCP server: `docker-compose up --build -d mcp-oauth`
- **Important**: When working on OAuth functionality, always rebuild `mcp-oauth` container, not `mcp`
- OAuth client credentials cached in `.nextcloud_oauth_shared_test_client.json`

**CI/CD Notes:**
- Playwright tests run in CI/CD environments
- Use Firefox browser in CI: `--browser firefox` (Chromium may have issues with localhost redirects)

### Configuration Files

- **`pyproject.toml`** - Python project configuration using uv for dependency management
- **`.env`** (from `env.sample`) - Environment variables for Nextcloud connection
- **`docker-compose.yml`** - Complete development environment with Nextcloud + database
