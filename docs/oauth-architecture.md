# OAuth Architecture

This document explains how OAuth2/OIDC authentication works in the Nextcloud MCP Server implementation.

## Overview

The Nextcloud MCP Server acts as an **OAuth 2.0 Resource Server**, protecting access to Nextcloud resources. It relies on Nextcloud's OIDC Identity Provider for user authentication and token validation.

## Architecture Diagram

```
┌─────────────┐                  ┌──────────────────┐                  ┌─────────────────┐
│             │                  │                  │                  │                 │
│ MCP Client  │                  │   MCP Server     │                  │   Nextcloud     │
│ (Claude,    │                  │   (Resource      │                  │   Instance      │
│  etc.)      │                  │    Server)       │                  │                 │
│             │                  │                  │                  │                 │
└──────┬──────┘                  └────────┬─────────┘                  └────────┬────────┘
       │                                  │                                     │
       │                                  │                                     │
       │  1. Connect to MCP               │                                     │
       ├─────────────────────────────────>│                                     │
       │                                  │                                     │
       │  2. Return auth settings         │                                     │
       │     (issuer_url, scopes)         │                                     │
       │<─────────────────────────────────┤                                     │
       │                                  │                                     │
       │                                  │                                     │
       │  3. Start OAuth flow (with PKCE) │                                     │
       ├──────────────────────────────────┼────────────────────────────────────>│
       │                                  │   /apps/oidc/authorize              │
       │                                  │                                     │
       │  4. User authenticates in browser│                                     │
       │<─────────────────────────────────┼─────────────────────────────────────┤
       │                                  │                                     │
       │  5. Authorization code (redirect)│                                     │
       │<─────────────────────────────────┤                                     │
       │                                  │                                     │
       │  6. Exchange code for token      │                                     │
       ├──────────────────────────────────┼────────────────────────────────────>│
       │                                  │   /apps/oidc/token                  │
       │                                  │                                     │
       │  7. Access token                 │                                     │
       │<─────────────────────────────────┼─────────────────────────────────────┤
       │                                  │                                     │
       │                                  │                                     │
       │  8. API request with Bearer token│                                     │
       ├─────────────────────────────────>│                                     │
       │     Authorization: Bearer xxx    │                                     │
       │                                  │                                     │
       │                                  │  9. Validate token via userinfo     │
       │                                  ├────────────────────────────────────>│
       │                                  │     /apps/oidc/userinfo             │
       │                                  │                                     │
       │                                  │  10. User info (token valid)        │
       │                                  │<────────────────────────────────────┤
       │                                  │                                     │
       │                                  │  11. Nextcloud API request          │
       │                                  ├────────────────────────────────────>│
       │                                  │     Authorization: Bearer xxx       │
       │                                  │     (Notes, Calendar, etc.)         │
       │                                  │                                     │
       │                                  │  12. API response                   │
       │                                  │<────────────────────────────────────┤
       │                                  │                                     │
       │  13. MCP tool response           │                                     │
       │<─────────────────────────────────┤                                     │
       │                                  │                                     │
```

## Components

### 1. MCP Client
- Any MCP-compatible client (Claude Desktop, Claude Code, custom clients)
- Initiates OAuth flow with PKCE (Proof Key for Code Exchange)
- Stores and sends access token with each request
- **Example**: Claude Desktop, Claude Code

### 2. MCP Server (Resource Server)
- **Role**: OAuth 2.0 Resource Server
- **Location**: This Nextcloud MCP Server implementation
- **Responsibilities**:
  - Validates Bearer tokens by calling Nextcloud's userinfo endpoint
  - Caches validated tokens (default: 1 hour TTL)
  - Creates authenticated Nextcloud client instances per-user
  - Enforces PKCE requirements (S256 code challenge method)
  - Exposes Nextcloud functionality via MCP tools

**Key Files**:
- [`app.py`](../nextcloud_mcp_server/app.py) - OAuth mode detection and configuration
- [`auth/token_verifier.py`](../nextcloud_mcp_server/auth/token_verifier.py) - Token validation logic
- [`auth/context_helper.py`](../nextcloud_mcp_server/auth/context_helper.py) - Per-user client creation

### 3. Nextcloud OIDC Apps

#### a) `oidc` - OIDC Identity Provider
- **Role**: OAuth 2.0 Authorization Server
- **Location**: Nextcloud app (`apps/oidc`)
- **Endpoints**:
  - `/.well-known/openid-configuration` - Discovery endpoint
  - `/apps/oidc/authorize` - Authorization endpoint
  - `/apps/oidc/token` - Token endpoint
  - `/apps/oidc/userinfo` - User info endpoint (token validation)
  - `/apps/oidc/jwks` - JSON Web Key Set
  - `/apps/oidc/register` - Dynamic client registration

**Configuration**:
```bash
# Enable dynamic client registration (optional)
# Settings → OIDC → "Allow dynamic client registration"
```

#### b) `user_oidc` - OpenID Connect User Backend
- **Role**: Bearer token validation middleware
- **Location**: Nextcloud app (`apps/user_oidc`)
- **Responsibilities**:
  - Validates Bearer tokens for Nextcloud API requests
  - Creates user sessions from valid Bearer tokens
  - Integrates with Nextcloud's authentication system

**Configuration**:
```bash
# Enable Bearer token validation (required)
php occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean
```

> [!IMPORTANT]
> The `user_oidc` app requires a patch to properly support Bearer token authentication for non-OCS endpoints. See [Upstream Status](oauth-upstream-status.md) for details.

### 4. Nextcloud Instance
- **Role**: Resource Owner / API Provider
- **Provides**: Notes, Calendar, Contacts, Deck, Files, etc.

## Authentication Flow

### Phase 1: OAuth Authorization (Steps 1-7)

1. **Client Connects**: MCP client connects to MCP server
2. **Auth Settings**: MCP server returns OAuth settings:
   ```json
   {
     "issuer_url": "https://nextcloud.example.com",
     "resource_server_url": "http://localhost:8000",
     "required_scopes": ["openid", "profile"]
   }
   ```
3. **OAuth Flow**: Client initiates OAuth flow with PKCE
   - Generates `code_verifier` (random string)
   - Calculates `code_challenge` = SHA256(code_verifier)
   - Redirects user to `/apps/oidc/authorize` with `code_challenge`
4. **User Authentication**: User logs in to Nextcloud via browser
5. **Authorization Code**: Nextcloud redirects back with authorization code
6. **Token Exchange**: Client exchanges code for access token
   - Sends `code` + `code_verifier` to `/apps/oidc/token`
   - OIDC app validates PKCE challenge
7. **Access Token**: Client receives access token (JWT or opaque)

### Phase 2: API Access (Steps 8-13)

8. **API Request**: Client sends MCP request with Bearer token
9. **Token Validation**: MCP server validates token:
   - Checks cache (1-hour TTL by default)
   - If not cached, calls `/apps/oidc/userinfo` with Bearer token
   - Extracts username from `sub` or `preferred_username` claim
10. **User Info**: Nextcloud returns user info if token is valid
11. **Nextcloud API Call**: MCP server calls Nextcloud API on behalf of user
    - Creates `NextcloudClient` instance with Bearer token
    - User-specific permissions apply
12. **API Response**: Nextcloud returns data
13. **MCP Response**: MCP server returns formatted response to client

## Token Validation

The MCP server validates tokens using the **userinfo endpoint approach**:

### Why Userinfo (vs JWT Validation)?

**Advantages**:
- Works with both JWT and opaque tokens
- No need to manage JWKS rotation
- Always up-to-date (respects token revocation)
- Simpler implementation

**Caching Strategy**:
- Validated tokens cached for 1 hour (configurable)
- Cache keyed by token string
- Expired tokens re-validated automatically

**Implementation**: See [`NextcloudTokenVerifier`](../nextcloud_mcp_server/auth/token_verifier.py)

## PKCE Requirement

The MCP server **requires** PKCE with S256 code challenge method:

1. Server validates OIDC discovery advertises PKCE support
2. Checks for `code_challenge_methods_supported` field
3. Verifies `S256` is included in supported methods
4. Logs error if PKCE not properly advertised

**Why PKCE?**:
- Required by MCP specification
- Protects against authorization code interception
- Essential for public clients (desktop apps, CLI tools)

**Implementation**: See [`validate_pkce_support()`](../nextcloud_mcp_server/app.py#L31-L93)

## Client Registration

The MCP server supports two client registration modes:

### Automatic Registration (Dynamic Client Registration)

```bash
# No client credentials needed
NEXTCLOUD_HOST=https://nextcloud.example.com
```

**How it works**:
1. Server checks `/.well-known/openid-configuration` for `registration_endpoint`
2. Calls `/apps/oidc/register` to register new client
3. Saves credentials to `.nextcloud_oauth_client.json`
4. Re-registers if credentials expire

**Best for**: Development, testing, short-lived deployments

### Pre-configured Client

```bash
# Manual client registration via CLI
php occ oidc:create --name="MCP Server" --type=confidential --redirect-uri="http://localhost:8000/oauth/callback"

# Configure MCP server
NEXTCLOUD_HOST=https://nextcloud.example.com
NEXTCLOUD_OIDC_CLIENT_ID=abc123
NEXTCLOUD_OIDC_CLIENT_SECRET=xyz789
```

**Best for**: Production, long-running deployments

## Per-User Client Instances

Each authenticated user gets their own `NextcloudClient` instance:

```python
# From MCP context (contains validated token)
client = get_client_from_context(ctx)

# Creates NextcloudClient with:
# - username: from token's 'sub' or 'preferred_username' claim
# - auth: BearerAuth(token)
```

**Benefits**:
- User-specific permissions
- Audit trail (actions appear from correct user)
- No shared credentials
- Multi-user support

**Implementation**: See [`get_client_from_context()`](../nextcloud_mcp_server/auth/context_helper.py)

## Security Considerations

### Token Storage
- MCP client stores access token
- MCP server does NOT store tokens (validates per-request)
- Token validation results cached in-memory only

### PKCE Protection
- Server validates PKCE is advertised
- Client MUST use PKCE with S256
- Protects against authorization code interception

### Scopes
- Required scopes: `openid`, `profile`
- Additional scopes inferred from userinfo response

### Token Validation
- Every MCP request validates Bearer token
- Cached for performance (1-hour default)
- Calls userinfo endpoint for validation

## Configuration

See [Configuration Guide](configuration.md) for all OAuth environment variables:

| Variable | Purpose |
|----------|---------|
| `NEXTCLOUD_HOST` | Nextcloud instance URL |
| `NEXTCLOUD_OIDC_CLIENT_ID` | Pre-configured client ID (optional) |
| `NEXTCLOUD_OIDC_CLIENT_SECRET` | Pre-configured client secret (optional) |
| `NEXTCLOUD_MCP_SERVER_URL` | MCP server URL for OAuth callbacks |
| `NEXTCLOUD_OIDC_CLIENT_STORAGE` | Path for auto-registered credentials |

## Testing

The integration test suite includes comprehensive OAuth testing:

- **Automated tests** (Playwright): [`tests/integration/test_oauth_playwright.py`](../tests/integration/test_oauth_playwright.py)
- **Interactive tests**: [`tests/integration/test_oauth_interactive.py`](../tests/integration/test_oauth_interactive.py)
- **Fixtures**: [`tests/conftest.py`](../tests/conftest.py)

Run OAuth tests:
```bash
# Start OAuth-enabled MCP server
docker-compose up --build -d mcp-oauth

# Run automated tests
uv run pytest tests/integration/test_oauth_playwright.py --browser firefox -v

# Run interactive tests (manual login)
uv run pytest tests/integration/test_oauth_interactive.py -v
```

## See Also

- [OAuth Setup Guide](oauth-setup.md) - Configuration steps
- [OAuth Quick Start](quickstart-oauth.md) - Get started quickly
- [Upstream Status](oauth-upstream-status.md) - Required upstream patches
- [OAuth Troubleshooting](oauth-troubleshooting.md) - Common issues
- [RFC 6749](https://www.rfc-editor.org/rfc/rfc6749) - OAuth 2.0 Authorization Framework
- [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636) - PKCE
- [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html)
