# OAuth Testing Setup

This document describes the automated OAuth testing infrastructure for the Nextcloud MCP server.

## Overview

We've created a comprehensive testing setup that includes:

1. **OIDC App Configuration** - Nextcloud OIDC app automatically installed and configured with dynamic client registration
2. **Dual MCP Services** - Two MCP server instances running in Docker:
   - `mcp` (port 8000) - BasicAuth mode (username/password)
   - `mcp-oauth` (port 8001) - OAuth mode (dynamic client registration)
3. **Test Fixtures** - Pytest fixtures for OAuth client testing
4. **Integration Tests** - OAuth-specific integration tests

## Docker Compose Setup

The `docker-compose.yml` includes:

```yaml
services:
  app:  # Nextcloud with OIDC app enabled
  mcp:  # BasicAuth MCP server (port 8000)
  mcp-oauth:  # OAuth MCP server (port 8001)
```

## OIDC Configuration

The OIDC app is configured automatically via `app-hooks/post-installation/install-oidc-app.sh`:

- **Dynamic Client Registration**: Enabled
- **Config Key**: `dynamic_client_registration` (not `allow_dynamic_client_registration`)
- **Registration Endpoint**: `http://localhost:8080/apps/oidc/register`

### Important: Config Key Fix

The correct OIDC config key is `dynamic_client_registration`. The initial implementation used `allow_dynamic_client_registration` which was incorrect and caused the registration endpoint to not appear in the OIDC discovery document.

## Test Fixtures

Located in `tests/conftest.py`:

### `oauth_token`
Session-scoped fixture that obtains an OAuth access token.

**Current Limitation**: Nextcloud OIDC only supports `authorization_code` and `refresh_token` grant types, not the `password` grant type. This means we cannot automatically obtain tokens for testing without implementing a full browser-based OAuth flow.

### `nc_oauth_client`
Session-scoped NextcloudClient configured with OAuth bearer token authentication.

**Status**: Implemented but currently skipped due to token acquisition limitation.

### `nc_mcp_oauth_client`
Session-scoped MCP client that connects to the OAuth-enabled MCP server on port 8001.

**Status**: Implemented but marked as skip - requires full OAuth authorization flow implementation in MCP SDK.

## Current Test Status

### ✅ Working
- OIDC app installation and configuration
- Dynamic client registration
- OAuth infrastructure (BearerAuth, TokenVerifier, client registration)
- Docker compose dual-mode setup

### ⚠️ Limitations
- **No automated token acquisition**: Nextcloud OIDC doesn't support the Resource Owner Password Credentials grant, which means we cannot programmatically get tokens for testing without browser interaction
- **Manual testing only**: OAuth functionality must be tested manually using a browser-based OAuth flow
- **MCP OAuth server untested**: The OAuth MCP server requires the full OAuth authorization flow to be implemented in the MCP Python SDK

## Manual Testing OAuth

To manually test OAuth functionality:

1. Start the docker-compose environment:
   ```bash
   docker-compose up -d
   ```

2. The OAuth MCP server runs on port 8001 and will:
   - Automatically register a client via dynamic registration
   - Store client credentials in `/app/.oauth/` volume
   - Display OAuth configuration on startup

3. To test OAuth with a real client:
   - Use the authorization endpoint: `http://localhost:8080/apps/oidc/authorize`
   - Implement the authorization code flow
   - Exchange code for token at: `http://localhost:8080/apps/oidc/token`

## Future Work

To enable automated OAuth testing, one of these approaches is needed:

1. **Mock OIDC Server**: Create a test OIDC server that supports password grant
2. **Browser Automation**: Use Selenium/Playwright to automate the OAuth flow
3. **Test-Only Password Grant**: Patch Nextcloud OIDC to support password grant in test mode
4. **Pre-generated Tokens**: Manually generate long-lived tokens and use them in tests

## Running Tests

```bash
# Run all tests (OAuth tests will be skipped)
uv run pytest tests/integration/test_oauth.py -v

# Run only the invalid token test (this one works)
uv run pytest tests/integration/test_oauth.py::TestOAuthTokenValidation::test_invalid_token_fails -v
```

## Files Modified

- `tests/conftest.py` - Added OAuth fixtures and token acquisition logic
- `tests/integration/test_oauth.py` - OAuth-specific integration tests
- `docker-compose.yml` - Added `mcp-oauth` service
- `app-hooks/post-installation/install-oidc-app.sh` - OIDC installation and configuration
- `nextcloud_mcp_server/client/__init__.py` - Added `from_token()` classmethod

## Notes

- The `from_token()` method was added to NextcloudClient to support OAuth authentication
- All OAuth infrastructure is in place and functional
- The main limitation is automated token acquisition for testing, not the OAuth implementation itself
