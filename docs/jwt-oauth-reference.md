# JWT OAuth Reference - Nextcloud MCP Server

**Last Updated:** 2025-10-23
**Status:** Production Ready

## Table of Contents

- [Overview](#overview)
- [JWT vs Opaque Tokens](#jwt-vs-opaque-tokens)
- [Scope-Based Authorization](#scope-based-authorization)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Production Deployment](#production-deployment)

---

## Overview

The Nextcloud MCP Server supports OAuth authentication with both **JWT** (RFC 9068) and **opaque** tokens. JWT tokens are recommended for production use as they enable:

- **Faster validation** - No HTTP call needed for token verification
- **Direct scope extraction** - Scopes embedded in token claims
- **Dynamic tool filtering** - Users only see tools they have permission to use
- **Signature verification** - Cryptographic validation using JWKS

### Key Features

✅ **JWT Token Support** - RFC 9068 compliant access tokens with RS256 signatures
✅ **Custom Scopes** - `nc:read` and `nc:write` for read/write access control
✅ **Dynamic Tool Filtering** - Tools filtered based on user's token scopes
✅ **Automatic Client Creation** - JWT OAuth clients auto-generated on container startup
✅ **Scope Challenges** - RFC-compliant `WWW-Authenticate` headers for insufficient scopes
✅ **Protected Resource Metadata** - RFC 8959 endpoint for scope discovery
✅ **Backward Compatible** - BasicAuth mode bypasses all scope checks

### Supported Scopes

| Scope | Description | Tool Count |
|-------|-------------|------------|
| `nc:read` | Read-only access to Nextcloud data | 36 tools |
| `nc:write` | Write access to create/modify/delete data | 54 tools |

All MCP tools (90 total) require at least one of these scopes. Standard OIDC scopes (`openid`, `profile`, `email`) are also supported.

---

## JWT vs Opaque Tokens

The Nextcloud OIDC app supports two token formats, configured per-client:

### JWT Tokens (Recommended)

**Advantages:**
- ✅ Fast validation - JWT signature verified locally using JWKS
- ✅ Direct scope extraction from `scope` claim in payload
- ✅ Standard approach (RFC 9068)
- ✅ No additional HTTP calls for validation

**Disadvantages:**
- ⚠️ Larger size (~800-1200 chars vs 72 chars for opaque)
- ⚠️ Token payload visible to client (not an issue for access tokens)

**Token Structure:**
```json
{
  "header": {
    "typ": "at+JWT",
    "alg": "RS256",
    "kid": "..."
  },
  "payload": {
    "iss": "http://localhost:8080",
    "sub": "admin",
    "aud": "client_id",
    "exp": 1234567890,
    "iat": 1234567890,
    "scope": "openid profile email nc:read nc:write",
    "client_id": "...",
    "jti": "..."
  }
}
```

### Opaque Tokens

**Advantages:**
- ✅ Smaller size (72 characters)
- ✅ No payload visible to client

**Disadvantages:**
- ❌ Requires userinfo endpoint call for each validation
- ❌ Scopes must be inferred from userinfo response (not directly available)
- ❌ Higher latency (HTTP call required)

**When to Use:**
- Use **JWT tokens** for production (better performance, direct scope access)
- Use **opaque tokens** for compatibility with clients that don't support JWT

---

## Scope-Based Authorization

### Scope Definitions

The MCP server uses **coarse-grained scopes** for simplicity:

| Scope | Operations | Examples |
|-------|------------|----------|
| `nc:read` | Read-only access | Get notes, search files, list calendars, read contacts |
| `nc:write` | Write operations | Create notes, update events, delete files, modify contacts |

### Standard OIDC Scopes

| Scope | Description | Required |
|-------|-------------|----------|
| `openid` | OIDC authentication | Yes |
| `profile` | User profile information | Recommended |
| `email` | Email address | Recommended |

### Recommended Configurations

**Full Access:**
```
openid profile email nc:read nc:write
```

**Read-Only:**
```
openid profile email nc:read
```

**No Custom Scopes (OIDC only):**
```
openid profile email
```

### Implementation

All 90 MCP tools are decorated with scope requirements:

```python
@mcp.tool()
@require_scopes("nc:read")
async def nc_notes_get_note(note_id: int, ctx: Context):
    """Get a note by ID (requires nc:read scope)"""
    ...

@mcp.tool()
@require_scopes("nc:write")
async def nc_notes_create_note(title: str, content: str, ctx: Context):
    """Create a note (requires nc:write scope)"""
    ...
```

**Coverage:**
- ✅ 36 read tools decorated with `@require_scopes("nc:read")`
- ✅ 54 write tools decorated with `@require_scopes("nc:write")`
- ✅ 90/90 tools covered (100%)

### Dynamic Tool Filtering

The MCP server implements **dynamic tool filtering** - users only see tools they have permission to use:

**JWT with `nc:read` only:**
- `list_tools()` returns 36 read-only tools
- Write tools are hidden from the tool list

**JWT with `nc:write` only:**
- `list_tools()` returns 54 write-only tools
- Read tools are hidden from the tool list

**JWT with both scopes:**
- `list_tools()` returns all 90 tools

**JWT with no custom scopes:**
- `list_tools()` returns 0 tools (all require `nc:read` or `nc:write`)

**BasicAuth mode:**
- `list_tools()` returns all 90 tools (no filtering)

### Scope Challenges

When a tool is called without required scopes, the server returns a `403 Forbidden` response with a `WWW-Authenticate` header:

```http
HTTP/1.1 403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope",
                  scope="nc:write",
                  resource_metadata="http://server/.well-known/oauth-protected-resource"
```

This enables **step-up authorization** - clients can detect missing scopes and trigger re-authentication to obtain additional permissions.

### Protected Resource Metadata (PRM)

The server implements RFC 8959's Protected Resource Metadata endpoint:

**Endpoint:** `GET /.well-known/oauth-protected-resource`

**Response:**
```json
{
  "resource": "http://localhost:8002",
  "scopes_supported": ["nc:read", "nc:write"],
  "authorization_servers": ["http://localhost:8080"],
  "bearer_methods_supported": ["header"],
  "resource_signing_alg_values_supported": ["RS256"]
}
```

This allows OAuth clients to discover supported scopes before requesting authorization.

---

## Configuration

### Docker Services

The development environment includes three MCP server variants:

| Service | Port | Auth Type | Token Type | Use Case |
|---------|------|-----------|------------|----------|
| `mcp` | 8000 | BasicAuth | N/A | Development, testing |
| `mcp-oauth` | 8001 | OAuth | Opaque | Standard OAuth flows |
| `mcp-oauth-jwt` | 8002 | OAuth | JWT | Production, JWT testing |

### JWT Service Configuration

The `mcp-oauth-jwt` service uses automatic client creation:

```yaml
mcp-oauth-jwt:
  build: .
  command: ["--transport", "streamable-http", "--oauth", "--port", "8002"]
  ports:
    - 127.0.0.1:8002:8002
  environment:
    - NEXTCLOUD_HOST=http://app:80
    - NEXTCLOUD_MCP_SERVER_URL=http://localhost:8002
    - NEXTCLOUD_PUBLIC_ISSUER_URL=http://localhost:8080
    - NEXTCLOUD_OIDC_CLIENT_STORAGE=/var/www/html/.oauth-jwt/nextcloud_oauth_client.json
    - NEXTCLOUD_OIDC_SCOPES=openid profile email nc:read nc:write
    - NEXTCLOUD_OIDC_TOKEN_TYPE=jwt
  volumes:
    - nextcloud:/var/www/html:ro
```

**Key Points:**
- No `NEXTCLOUD_OIDC_CLIENT_ID` or `CLIENT_SECRET` needed (loaded from storage)
- JWT client auto-created during Nextcloud initialization
- Credentials stored in `/var/www/html/.oauth-jwt/nextcloud_oauth_client.json`
- Volume mounted read-only for security

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NEXTCLOUD_HOST` | Nextcloud base URL | `http://localhost:8080` |
| `NEXTCLOUD_MCP_SERVER_URL` | MCP server external URL | (required) |
| `NEXTCLOUD_PUBLIC_ISSUER_URL` | Public issuer URL for JWT validation | (uses `NEXTCLOUD_HOST`) |
| `NEXTCLOUD_OIDC_CLIENT_STORAGE` | Path to client credentials JSON | (optional - uses DCR if unset) |
| `NEXTCLOUD_OIDC_SCOPES` | Space-separated scopes to request | `"openid profile email nc:read nc:write"` |
| `NEXTCLOUD_OIDC_TOKEN_TYPE` | Token format: `"jwt"` or `"Bearer"` | `"Bearer"` |

### Manual Client Creation

If not using automatic creation, create a JWT client manually:

```bash
docker compose exec app php occ oidc:create \
  --token_type=jwt \
  --allowed_scopes="openid profile email nc:read nc:write" \
  "Nextcloud MCP Server" \
  "http://localhost:8002/oauth/callback"
```

**Output:**
```json
{
  "client_id": "...",
  "client_secret": "...",
  "token_type": "jwt",
  "allowed_scopes": "openid profile email nc:read nc:write"
}
```

Then configure the MCP server:
```bash
export NEXTCLOUD_OIDC_CLIENT_ID="<client_id>"
export NEXTCLOUD_OIDC_CLIENT_SECRET="<client_secret>"
export NEXTCLOUD_OIDC_TOKEN_TYPE="jwt"
```

---

## Architecture

### Component Overview

```
┌──────────────────┐     OAuth Flow      ┌──────────────────┐
│  OAuth Client    │<─────────────────────>│ Nextcloud OIDC  │
│  (Claude, etc)   │                       │ Server           │
└────────┬─────────┘                       └────────┬─────────┘
         │                                          │
         │ JWT Access Token                         │
         │ {                                        │
         │   "scope": "openid nc:read nc:write"    │
         │   ...                                    │
         │ }                                        │
         │                                          │
         v                                          │
┌────────────────────────────────────────────────────────────┐
│         Nextcloud MCP Server                               │
│  ┌───────────────────────────────────────────────────┐    │
│  │  NextcloudTokenVerifier                            │    │
│  │  - JWT signature verification (JWKS)               │    │
│  │  - Scope extraction from token payload             │    │
│  │  - Fallback to userinfo endpoint (opaque tokens)   │    │
│  └───────────────────┬───────────────────────────────┘    │
│                      │                                     │
│                      v                                     │
│  ┌───────────────────────────────────────────────────┐    │
│  │  Dynamic Tool Filtering (list_tools)               │    │
│  │  - Get user scopes from verified token             │    │
│  │  - Filter tools based on @require_scopes metadata  │    │
│  │  - Return only accessible tools                     │    │
│  └───────────────────┬───────────────────────────────┘    │
│                      │                                     │
│                      v                                     │
│  ┌───────────────────────────────────────────────────┐    │
│  │  Tool Execution (@require_scopes decorator)        │    │
│  │  - Check token scopes before execution             │    │
│  │  - Raise InsufficientScopeError if missing         │    │
│  │  - Return 403 with WWW-Authenticate header         │    │
│  └───────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

### Key Components

**1. Token Verification** (`nextcloud_mcp_server/auth/token_verifier.py`)
- JWT signature verification using JWKS (RS256)
- Scope extraction from `scope` claim
- Token caching with TTL
- Fallback to userinfo endpoint for opaque tokens

**2. Scope Authorization** (`nextcloud_mcp_server/auth/scope_authorization.py`)
- `@require_scopes()` decorator for tools
- `get_required_scopes()` - Extract scope requirements from functions
- `has_required_scopes()` - Check if user has necessary scopes
- `InsufficientScopeError` exception for WWW-Authenticate challenges

**3. Dynamic Filtering** (`nextcloud_mcp_server/app.py:433-488`)
- Overrides FastMCP's `list_tools()` method
- Filters based on user's JWT token scopes
- Only active in OAuth mode
- Bypassed in BasicAuth mode

**4. PRM Endpoint** (`nextcloud_mcp_server/app.py:503-532`)
- `GET /.well-known/oauth-protected-resource`
- Advertises `["nc:read", "nc:write"]`
- RFC 8959 compliant

**5. Exception Handler** (`nextcloud_mcp_server/app.py:540-563`)
- Catches `InsufficientScopeError`
- Returns 403 with `WWW-Authenticate` header
- Includes missing scopes and PRM endpoint URL

### Automatic Client Creation

**Post-Installation Hook:** `app-hooks/post-installation/90-create-jwt-oauth-client.sh`

On container startup, this script:
1. Installs/enables OIDC app
2. Creates JWT client via `occ oidc:create --token_type=jwt`
3. Parses JSON output for credentials
4. Saves to `/var/www/html/.oauth-jwt/nextcloud_oauth_client.json`

**Credential Format:**
```json
{
  "client_id": "XBd2xqIisu3Kswg39Ub4BUhC36PEYjwwivx3G5nZdDgigvwKXrTHozs7m9DeoLSY",
  "client_secret": "xNKcy0qpUSau36T60pGGdb03pMEVLXtqykxjK8YkDpoNxNcZ4ClyAT3IAEse2AKT",
  "client_id_issued_at": 1761097039,
  "client_secret_expires_at": 2076457039,
  "redirect_uris": ["http://localhost:8002/oauth/callback"]
}
```

---

## Testing

### Test Infrastructure

The test suite includes comprehensive coverage for JWT OAuth and scope authorization:

**Test Files:**
- `tests/server/test_scope_authorization.py` - Scope-based authorization tests (4 tests)
- `tests/server/test_mcp_oauth_jwt.py` - JWT OAuth integration tests
- `tests/conftest.py` - Shared fixtures for JWT testing

### Consent Scenario Tests

Four test scenarios verify scope-based tool filtering with different consent levels:

#### 1. No Custom Scopes (0 tools)
```bash
uv run pytest tests/server/test_scope_authorization.py::test_jwt_with_no_custom_scopes_returns_zero_tools -v
```

**Scenario:** JWT token with only OIDC defaults (`openid profile email`)
**Expected:** 0 tools returned (all require `nc:read` or `nc:write`)
**Verifies:** Security - users who decline custom scopes cannot access any MCP tools

#### 2. Read-Only Access (36 tools)
```bash
uv run pytest tests/server/test_scope_authorization.py::test_jwt_consent_scenarios_read_only -v
```

**Scenario:** JWT token with `nc:read` only
**Expected:** 36 read-only tools visible, write tools hidden
**Verifies:** Read tools accessible, write tools filtered out

#### 3. Write-Only Access (54 tools)
```bash
uv run pytest tests/server/test_scope_authorization.py::test_jwt_consent_scenarios_write_only -v
```

**Scenario:** JWT token with `nc:write` only
**Expected:** 54 write tools visible, read tools hidden
**Verifies:** Write tools accessible, read tools filtered out

#### 4. Full Access (90 tools)
```bash
uv run pytest tests/server/test_scope_authorization.py::test_jwt_consent_scenarios_full_access -v
```

**Scenario:** JWT token with both `nc:read` and `nc:write`
**Expected:** All 90 tools visible
**Verifies:** Full access when user grants all custom scopes

### Test Fixtures

**OAuth Client Fixtures:**
- `read_only_oauth_client_credentials` - Client with `nc:read` only
- `write_only_oauth_client_credentials` - Client with `nc:write` only
- `full_access_oauth_client_credentials` - Client with both scopes
- `no_custom_scopes_oauth_client_credentials` - Client with OIDC defaults only

**Token Fixtures:**
- `playwright_oauth_token_read_only` - Obtains token with `nc:read`
- `playwright_oauth_token_write_only` - Obtains token with `nc:write`
- `playwright_oauth_token_full_access` - Obtains token with both scopes
- `playwright_oauth_token_no_custom_scopes` - Obtains token with no custom scopes

**MCP Client Fixtures:**
- `nc_mcp_oauth_client_read_only` - MCP session with read-only token
- `nc_mcp_oauth_client_write_only` - MCP session with write-only token
- `nc_mcp_oauth_client_full_access` - MCP session with full access token
- `nc_mcp_oauth_client_no_custom_scopes` - MCP session with no custom scopes

### Running Tests

**All consent scenario tests:**
```bash
uv run pytest tests/server/test_scope_authorization.py -v
```

**JWT OAuth integration tests:**
```bash
uv run pytest tests/server/test_mcp_oauth_jwt.py -v --browser firefox
```

**With visible browser (debugging):**
```bash
uv run pytest tests/server/test_mcp_oauth_jwt.py -v --browser firefox --headed
```

### Test Configuration

**Playwright Browser:**
- Default: Chromium
- Recommended for CI: Firefox (`--browser firefox`)
- Debugging: Add `--headed` flag

**OAuth Flow:**
- Uses automated Playwright browser automation
- Completes OAuth consent flow programmatically
- Creates separate OAuth client for each scenario
- Each user gets unique access token

---

## Troubleshooting

### Issue: JWT Issuer Validation Failed

**Symptom:**
```
WARNING JWT issuer validation failed: Invalid issuer
WARNING JWT verification failed, will try other methods
✅ Extracted scopes from access token: {'openid', 'profile'}
```

**Cause:** Token's `iss` claim doesn't match expected issuer URL. This often happens when:
- Using `localhost` vs `127.0.0.1` inconsistently
- MCP server uses internal URL but clients use public URL

**Solution:**
```bash
# Option 1: Use consistent URLs
export NEXTCLOUD_PUBLIC_ISSUER_URL=http://localhost:8080
# Ensure all test fixtures also use localhost:8080

# Option 2: Check discovery document
curl http://localhost:8080/.well-known/openid-configuration | jq .issuer
# Use this exact issuer in NEXTCLOUD_PUBLIC_ISSUER_URL
```

**Impact if not fixed:**
- JWT validation falls back to userinfo endpoint
- Scopes inferred from userinfo (only standard OIDC scopes, no custom scopes)
- Result: 0 tools visible or incorrect tool filtering

### Issue: Scopes Not Present in JWT

**Symptom:** JWT token doesn't contain `scope` claim or contains empty string

**Cause:** Client's `allowed_scopes` is empty or not configured

**Solution:**
```bash
# Check client configuration
docker compose exec app php occ oidc:list

# Look for allowed_scopes in output
# If empty, recreate client with --allowed_scopes
docker compose exec app php occ oidc:create \
  --token_type=jwt \
  --allowed_scopes="openid profile email nc:read nc:write" \
  "Client Name" \
  "http://callback/url"
```

### Issue: All Tools Visible Despite Read-Only Token

**Symptom:** User with `nc:read` token can see all 90 tools including write tools

**Cause:** Server running in BasicAuth mode, not OAuth mode

**Solution:**
```bash
# Verify OAuth mode is active
docker compose logs mcp-oauth-jwt | grep "OAuth mode"

# Should see: "Running in OAuth mode"

# If not, check environment variables:
docker compose exec mcp-oauth-jwt env | grep NEXTCLOUD_OIDC

# Ensure no NEXTCLOUD_USERNAME or NEXTCLOUD_PASSWORD set
```

### Issue: Dynamic Client Registration Doesn't Set Scopes

**Symptom:** Client registered via DCR has empty `allowed_scopes`

**Cause:** OIDC app's registration endpoint doesn't populate `allowed_scopes` from request

**Workaround:** Use pre-configured clients instead of DCR for JWT tokens:
```bash
# Create client manually via occ
docker compose exec app php occ oidc:create \
  --token_type=jwt \
  --allowed_scopes="openid profile email nc:read nc:write" \
  "Client Name" \
  "http://callback"

# Configure MCP server to use these credentials
export NEXTCLOUD_OIDC_CLIENT_ID="..."
export NEXTCLOUD_OIDC_CLIENT_SECRET="..."
```

### Issue: Token Type Case Sensitivity

**Symptom:** JWT tokens not generated even though `token_type=JWT` set

**Cause:** OIDC app checks `token_type === 'jwt'` (lowercase)

**Solution:** Always use lowercase:
```bash
# Correct
export NEXTCLOUD_OIDC_TOKEN_TYPE=jwt

# Incorrect (will generate opaque tokens)
export NEXTCLOUD_OIDC_TOKEN_TYPE=JWT
```

### Issue: Missing WWW-Authenticate Header

**Symptom:** 403 error doesn't include `WWW-Authenticate` header

**Cause:** Server not in OAuth mode, or exception not being caught

**Solution:**
```bash
# Check server logs for OAuth mode
docker compose logs mcp-oauth-jwt | grep "WWW-Authenticate scope challenges enabled"

# Should see this during startup

# Check exception handling
docker compose logs mcp-oauth-jwt | grep "InsufficientScopeError"
```

### Debugging Tools

**Check JWT contents:**
```bash
# Decode JWT (base64 decode the payload)
echo "JWT_PAYLOAD_PART" | base64 -d | jq .
```

**Check database scopes:**
```bash
# View access tokens with scopes
docker compose exec db mariadb -u nextcloud -ppassword nextcloud \
  -e "SELECT id, client_id, user_id, scope FROM oc_oidc_access_tokens ORDER BY id DESC LIMIT 5;"

# View user consents
docker compose exec db mariadb -u nextcloud -ppassword nextcloud \
  -e "SELECT user_id, client_id, scopes_granted FROM oc_oidc_user_consents;"
```

**Check server logs:**
```bash
# Follow JWT verification logs
docker compose logs -f mcp-oauth-jwt | grep -E "JWT|scope|tool"

# Check for issuer mismatches
docker compose logs mcp-oauth-jwt | grep -i issuer
```

---

## Production Deployment

### Deployment Checklist

✅ **Use JWT Tokens** - Enable `token_type=jwt` for better performance
✅ **Configure Allowed Scopes** - Always set `allowed_scopes` on OAuth clients
✅ **Use Pre-Configured Clients** - Avoid DCR limitation with manual client creation
✅ **Consistent URLs** - Use same URL for `NEXTCLOUD_HOST` and `PUBLIC_ISSUER_URL`
✅ **Secure Credentials** - Store client credentials securely (environment variables or secrets management)
✅ **Monitor Token Size** - JWT tokens are 10-15x larger than opaque (not usually an issue)
✅ **Enable Logging** - Configure appropriate log levels for JWT verification

### Production Configuration Example

```yaml
# docker-compose.yml (production)
mcp-oauth-jwt:
  image: ghcr.io/yourusername/nextcloud-mcp-server:latest
  environment:
    - NEXTCLOUD_HOST=https://nextcloud.example.com
    - NEXTCLOUD_MCP_SERVER_URL=https://mcp.example.com
    - NEXTCLOUD_PUBLIC_ISSUER_URL=https://nextcloud.example.com
    - NEXTCLOUD_OIDC_CLIENT_ID=${JWT_CLIENT_ID}
    - NEXTCLOUD_OIDC_CLIENT_SECRET=${JWT_CLIENT_SECRET}
    - NEXTCLOUD_OIDC_SCOPES=openid profile email nc:read nc:write
    - NEXTCLOUD_OIDC_TOKEN_TYPE=jwt
  ports:
    - "8002:8002"
```

### Security Considerations

**Token Storage:**
- Never commit credentials to version control
- Use environment variables or secrets management
- Rotate client secrets periodically

**Scope Configuration:**
- Grant minimum necessary scopes to clients
- Use read-only tokens for AI assistants that don't need write access
- Review OAuth client list regularly

**Network Security:**
- Use HTTPS in production
- Ensure issuer URL matches public URL
- Configure proper CORS headers

### Monitoring

**Key Metrics:**
- JWT verification success/failure rate
- Scope challenge frequency (indicates clients with insufficient scopes)
- Token validation latency
- Tool execution by scope (identify unused scopes)

**Log Patterns:**
```bash
# Success
INFO JWT verified successfully for user: admin
INFO ✅ Extracted scopes from access token: {'openid', 'profile', 'email', 'nc:read', 'nc:write'}

# Failures
WARNING JWT issuer validation failed: Invalid issuer
WARNING Missing required scopes: nc:write
```

### Known Limitations

1. **No Fine-Grained Scopes** - Only coarse `nc:read` and `nc:write` (not per-app scopes)
2. **DCR Doesn't Set Allowed Scopes** - Must use pre-configured clients for JWT
3. **No Introspection Endpoint** - OIDC app lacks RFC 7662 introspection
4. **No Refresh Token Support** - Tokens must be reacquired when expired

### Future Enhancements

**Potential Improvements:**
- Per-app scopes (`nc:notes:read`, `nc:calendar:write`)
- Resource-level filtering (apply to MCP resources, not just tools)
- Automatic scope discovery from decorated tools
- Admin UI for scope management
- Token introspection endpoint (RFC 7662)

---

## References

### Standards

- [RFC 9068: JWT Profile for OAuth 2.0 Access Tokens](https://www.rfc-editor.org/rfc/rfc9068.html)
- [RFC 7519: JSON Web Token (JWT)](https://www.rfc-editor.org/rfc/rfc7519.html)
- [RFC 7517: JSON Web Key (JWK)](https://www.rfc-editor.org/rfc/rfc7517.html)
- [RFC 8959: Protected Resource Metadata](https://www.rfc-editor.org/rfc/rfc8959.html)
- [RFC 7662: OAuth 2.0 Token Introspection](https://www.rfc-editor.org/rfc/rfc7662.html)

### Related Documentation

- [OAuth Setup Guide](oauth-setup.md) - Complete OAuth configuration guide
- [OAuth Architecture](oauth-architecture.md) - Detailed architecture documentation
- [OAuth Troubleshooting](oauth-troubleshooting.md) - Common OAuth issues and solutions
- [Authentication Guide](authentication.md) - BasicAuth vs OAuth comparison

### External Resources

- [Nextcloud OIDC App](https://github.com/H2CK/oidc) - OIDC identity provider for Nextcloud
- [PyJWT Documentation](https://pyjwt.readthedocs.io/) - JWT library used for verification
- [FastMCP Documentation](https://github.com/jlowin/fastmcp) - MCP server framework

---

**Implementation Date:** 2025-10-21 to 2025-10-23
**Version:** 1.0.0
**Status:** ✅ Production Ready
