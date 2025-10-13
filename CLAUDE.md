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

# After code changes, rebuild and restart only the MCP server container
docker-compose up --build -d mcp

# Build Docker image
docker build -t nextcloud-mcp-server .
```

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

- **Integration tests** in `tests/integration/` - Test real Nextcloud API interactions
- **Fixtures** in `tests/conftest.py` - Shared test setup and utilities
- Tests are marked with `@pytest.mark.integration` for selective running
- **Important**: Integration tests run against live Docker containers. After making code changes to the MCP server, rebuild only the MCP container with `docker-compose up --build -d mcp` before running tests

#### Testing Best Practices
- **MANDATORY: Always run tests after implementing features or fixing bugs**
  - Run tests to completion before considering any task complete
  - If tests require modifications to pass, ask for permission before proceeding
  - Use `docker-compose up --build -d mcp` to rebuild MCP container after code changes
- **Use existing fixtures** from `tests/conftest.py` to avoid duplicate setup work:
  - `nc_mcp_client` - MCP client session for tool/resource testing
  - `nc_client` - Direct NextcloudClient for setup/cleanup operations
  - `temporary_note` - Creates and cleans up test notes automatically
  - `temporary_addressbook` - Creates and cleans up test address books
  - `temporary_contact` - Creates and cleans up test contacts
- **Test specific functionality** after changes:
  - For Notes changes: `uv run pytest tests/integration/test_mcp.py -k "notes" -v`
  - For specific API changes: `uv run pytest tests/integration/test_notes_api.py -v`
- **Avoid creating standalone test scripts** - use pytest with proper fixtures instead

#### OAuth/OIDC Testing
OAuth integration tests support both **automated** (Playwright) and **interactive** authentication flows:

**Automated Testing (Default - Recommended for CI/CD):**
- **Default fixtures**: `nc_oauth_client`, `nc_mcp_oauth_client` now use Playwright automation by default
- Uses Playwright headless browser automation to complete OAuth flow programmatically
- All Playwright fixtures: `playwright_oauth_token`, `nc_oauth_client`, `nc_mcp_oauth_client`, `nc_oauth_client_playwright`, `nc_mcp_oauth_client_playwright`
- Requires: `NEXTCLOUD_HOST`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD` environment variables
- Uses `pytest-playwright-asyncio` for async Playwright fixtures
- Playwright configuration: Use pytest CLI args like `--browser firefox --headed` to customize
- Install browsers: `uv run playwright install firefox` (or `chromium`, `webkit`)
- Example:
  ```bash
  # Run all OAuth tests with automated Playwright flow using Firefox
  uv run pytest tests/integration/test_oauth*.py --browser firefox -v

  # Run specific Playwright tests with visible browser for debugging
  uv run pytest tests/integration/test_oauth_playwright.py --browser firefox --headed -v

  # Run with Chromium (default)
  uv run pytest tests/integration/test_oauth.py -v
  ```

**Interactive Testing (Manual browser login):**
- Opens system browser and waits for manual login/authorization
- Fixtures: `interactive_oauth_token`, `nc_oauth_client_interactive`, `nc_mcp_oauth_client_interactive`
- Requires: User to complete browser-based login when prompted
- Useful for: Debugging OAuth flows, testing with 2FA, local development
- **Automatically skipped in GitHub Actions CI** - Interactive fixtures check for `GITHUB_ACTIONS` environment variable
- Example:
  ```bash
  # Run OAuth tests with interactive flow (will open browser and wait for manual login)
  uv run pytest tests/integration/test_oauth_interactive.py -v
  ```

**Test Environment Setup:**
- Start OAuth MCP server: `docker-compose up --build -d mcp-oauth`
- OAuth server runs on port 8001 (regular MCP on 8000)
- Both flows register OAuth clients dynamically using Nextcloud's OIDC provider

**CI/CD Considerations:**
- Interactive OAuth tests are automatically skipped when `GITHUB_ACTIONS` environment variable is set
- Automated Playwright tests will run in CI/CD environments
- Use Firefox browser in CI: `--browser firefox` (Chromium may have issues with localhost redirects)

### Configuration Files

- **`pyproject.toml`** - Python project configuration using uv for dependency management
- **`.env`** (from `env.sample`) - Environment variables for Nextcloud connection
- **`docker-compose.yml`** - Complete development environment with Nextcloud + database
