# OAuth2/OIDC Implementation Plan for Nextcloud MCP Server

## Executive Summary
Upgrade the Nextcloud MCP server to support OAuth2/OIDC authentication using Nextcloud's OIDC app as the Authorization Server, eliminating the need for baked-in credentials in server deployment.

**Status**: ✅ Research Complete - Implementation Ready

## Research Findings Summary

### ✅ Verified Nextcloud OIDC Capabilities
- **Token Format**: Opaque tokens by default, **RFC 9068 JWT access tokens available** (must be enabled per-client)
- **Discovery**: Full OpenID Connect discovery available at `/.well-known/openid-configuration`
- **JWKS**: Available at `/apps/oidc/jwks` for JWT signature validation
- **Dynamic Registration**: Supported via `/apps/oidc/register` (must be enabled by admin)
- **Introspection**: ❌ NOT available - must use **userinfo endpoint** for token validation
- **Userinfo**: Available at `/apps/oidc/userinfo` - validates token and returns user claims
- **Scopes**: `openid`, `profile`, `email`, `roles`, `groups`
- **User Claims**: `sub`, `preferred_username` (both contain Nextcloud username)

### 🔑 Key Implementation Decisions
1. **Primary Token Validation**: Use **userinfo endpoint** (not introspection)
2. **JWT Support**: Optional - enables local validation if client configured for RFC 9068
3. **User Context**: Extract username from `sub` or `preferred_username` claim via userinfo
4. **Dynamic Registration**: Primary deployment method (zero-config)
5. **Token Lifetime**: Access tokens default to 3600s, clients default to 3600s (both configurable)

## Architecture Overview

### Server Role: Resource Server (RS) - RFC 9728
The MCP server acts as a **Resource Server** that:
- Validates OAuth tokens issued by Nextcloud OIDC app (Authorization Server)
- Protects MCP tools/resources with OAuth authentication
- Uses validated tokens to make Nextcloud API calls on behalf of authenticated users

### Authentication Flow
```
1. Client connects to MCP Server (RS)
2. MCP Server provides RFC 9728 metadata pointing to Nextcloud OIDC (AS)
3. Client performs OAuth flow with Nextcloud OIDC
4. Client presents access token to MCP Server
5. MCP Server validates token via userinfo endpoint (or JWT if configured)
6. MCP Server extracts username from claims
7. MCP Server uses token to call Nextcloud APIs with user context
```

## Key Design Decisions

### 1. Dynamic Client Registration (PRIMARY APPROACH)
**Use Nextcloud OIDC's Dynamic Client Registration for zero-config deployment**

**Benefits:**
- No manual client setup required
- MCP server auto-registers on first startup
- Automatic credential generation
- Self-healing if client expires
- Better developer/deployment experience

**Implementation:**
```python
# Startup sequence:
1. Check for existing client credentials (file/env)
2. If none found, POST to /apps/oidc/register
3. Store client_id and client_secret persistently
4. Use credentials for OAuth flow
5. Auto re-register if client expires (3600s default)
```

**Nextcloud OIDC Requirements:**
- Admin must enable "Dynamic Client Registration" in OIDC app settings
- Rate limiting via BruteForce protection
- Max 100 dynamic clients per instance
- Clients expire after 1 hour (configurable via occ)

### 2. Token Validation Strategy: Userinfo Endpoint (PRIMARY)

**✅ VERIFIED IMPLEMENTATION: Userinfo Endpoint Validation**

Nextcloud OIDC **does NOT provide** a token introspection endpoint. Token validation must use:

**Primary: Userinfo Endpoint Validation**
- Call `/apps/oidc/userinfo` with Bearer token
- Nextcloud validates token internally (checks expiration, client, etc.)
- Returns user claims if valid: `sub`, `preferred_username`, `email`, `roles`, `groups`
- HTTP 400/401 if token invalid
- Cache results with TTL matching token expiration (3600s default)

**Implementation Pattern**:
```python
async def verify_token(self, token: str) -> AccessToken | None:
    # Call userinfo endpoint
    response = await client.get(
        f"{nextcloud_host}/apps/oidc/userinfo",
        headers={"Authorization": f"Bearer {token}"}
    )

    if response.status_code == 200:
        claims = response.json()
        return AccessToken(
            token=token,
            client_id="",  # Not available from userinfo
            scopes=["openid", "profile"],  # From original request
            expires_at=calculate_expiry()  # 3600s from now
        )
    return None  # Invalid token
```

**Optional: JWT Validation (Performance Optimization)**
- Available if client configured with "JWT Access Tokens (RFC 9068)" enabled
- Fetch JWKS from `/apps/oidc/jwks`
- Validate JWT signatures locally (no network call)
- Cache JWKS with refresh mechanism
- Falls back to userinfo if JWT validation fails

**Trade-offs**:
- Userinfo: Simpler, always works, network call per validation
- JWT: Faster, no network call, requires per-client configuration

### 3. Dual-Mode Authentication (Backward Compatibility)
Support both authentication modes:

**Mode 1: OAuth2/OIDC (NEW)**
- Environment: `NEXTCLOUD_HOST` + optional `NEXTCLOUD_OIDC_CLIENT_ID/SECRET`
- Auto-registers if no client credentials provided
- Per-request client creation with bearer token

**Mode 2: Basic Auth (LEGACY)**
- Environment: `NEXTCLOUD_HOST` + `NEXTCLOUD_USERNAME` + `NEXTCLOUD_PASSWORD`
- Current implementation preserved
- Single client in lifespan context

### 4. HTTP Client Architecture

**✅ REVISED: Context-aware Client Retrieval**

Instead of per-request client creation, use a helper that extracts user context:

```python
# Helper function to get client from MCP context
async def get_client_from_context(ctx: Context, base_url: str) -> NextcloudClient:
    """Extract authenticated user context and create NextcloudClient."""
    # MCP SDK provides AccessToken from TokenVerifier
    access_token: AccessToken = ctx.request_context.session.access_token

    # Extract username from cached userinfo claims
    # (stored during token verification)
    username = access_token.scopes[0]  # Or from custom metadata

    # Create client with bearer token
    return NextcloudClient.from_token(
        base_url=base_url,
        token=access_token.token,
        username=username
    )

# In tool implementations:
@mcp.tool()
async def nc_notes_create(title: str, content: str):
    ctx = mcp.get_context()

    if oauth_mode:
        client = await get_client_from_context(ctx, nextcloud_host)
    else:
        # Legacy: use lifespan client
        client = ctx.request_context.lifespan_context.client

    return await client.notes.create_note(title, content)
```

**Key Pattern**:
- Token verification caches userinfo claims
- Helper retrieves username from cached data (no additional API call)
- Client uses bearer token for Nextcloud API calls

### 5. User Context Extraction

**✅ VERIFIED: Userinfo Endpoint Response**

From Nextcloud OIDC userinfo endpoint response:
- **Username**: `sub` AND `preferred_username` (both contain Nextcloud username)
- **Scopes**: Determined by scopes requested during OAuth flow
- **Groups/Roles**: Available via `roles` or `groups` scope
- **Profile**: `name`, `email`, `picture`, etc. (if `profile` scope requested)

**Implementation**:
```python
# During token verification:
userinfo = await fetch_userinfo(token)
# {
#   "sub": "username",
#   "preferred_username": "username",
#   "email": "user@example.com",
#   "roles": ["group1", "group2"],  # if 'roles' scope
#   "groups": ["group1", "group2"]  # if 'groups' scope
# }

username = userinfo["sub"]  # or userinfo["preferred_username"]
```

**Storage Strategy**:
- Cache userinfo in AccessToken metadata
- Use MCP SDK's built-in token caching
- TTL matches access token expiration (3600s default)

## Implementation Components

### New Modules

#### 1. `nextcloud_mcp_server/auth/__init__.py`
Exports: `NextcloudTokenVerifier`, `BearerAuth`, `register_client`

#### 2. `nextcloud_mcp_server/auth/token_verifier.py`
```python
class NextcloudTokenVerifier(TokenVerifier):
    """
    Validates access tokens using Nextcloud OIDC userinfo endpoint.

    Primary method: Userinfo endpoint validation (always works)
    Optional: JWT validation if client configured for RFC 9068
    """

    def __init__(
        self,
        nextcloud_host: str,
        userinfo_uri: str,
        jwks_uri: str | None = None,
        enable_jwt_validation: bool = False
    ):
        self.nextcloud_host = nextcloud_host
        self.userinfo_uri = userinfo_uri
        self.jwks_uri = jwks_uri
        self.enable_jwt_validation = enable_jwt_validation

        # Cache for validated tokens: token -> (userinfo, expiry)
        self._token_cache: dict[str, tuple[dict, float]] = {}

        # JWKS cache (if JWT validation enabled)
        self._jwks: dict | None = None
        self._jwks_expires: float = 0

        self._client = httpx.AsyncClient()

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Verify token using userinfo endpoint (primary) or JWT validation (optional).

        Returns AccessToken with userinfo cached in metadata.
        """
        # Check cache first
        if token in self._token_cache:
            userinfo, expiry = self._token_cache[token]
            if time.time() < expiry:
                return self._create_access_token(token, userinfo)

        # Try JWT validation first if enabled
        if self.enable_jwt_validation and self.jwks_uri:
            access_token = await self._verify_jwt(token)
            if access_token:
                return access_token

        # Fall back to (or use primary) userinfo validation
        return await self._verify_via_userinfo(token)

    async def _verify_via_userinfo(self, token: str) -> AccessToken | None:
        """Validate token by calling userinfo endpoint."""
        try:
            response = await self._client.get(
                self.userinfo_uri,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0
            )

            if response.status_code == 200:
                userinfo = response.json()

                # Cache for 3600s (default token lifetime)
                # TODO: Get actual expiry from token if JWT
                expiry = time.time() + 3600
                self._token_cache[token] = (userinfo, expiry)

                return self._create_access_token(token, userinfo)

        except Exception as e:
            logger.warning(f"Userinfo validation failed: {e}")

        return None

    async def _verify_jwt(self, token: str) -> AccessToken | None:
        """Validate JWT token locally using JWKS (optional optimization)."""
        try:
            # Fetch JWKS if not cached
            if not self._jwks or time.time() > self._jwks_expires:
                await self._refresh_jwks()

            # Decode and validate JWT
            claims = jwt.decode(
                token,
                self._jwks,
                algorithms=["RS256", "HS256"],
                issuer=self.nextcloud_host,
                options={"verify_aud": False}  # Nextcloud may not include aud
            )

            # Extract userinfo from JWT claims
            userinfo = {
                "sub": claims.get("sub"),
                "preferred_username": claims.get("preferred_username"),
                "email": claims.get("email"),
                "roles": claims.get("roles", []),
                "groups": claims.get("groups", [])
            }

            # Cache
            expiry = claims.get("exp", time.time() + 3600)
            self._token_cache[token] = (userinfo, expiry)

            return self._create_access_token(token, userinfo)

        except Exception as e:
            logger.debug(f"JWT validation failed, falling back to userinfo: {e}")
            return None

    def _create_access_token(self, token: str, userinfo: dict) -> AccessToken:
        """Create AccessToken with userinfo in metadata."""
        username = userinfo.get("sub") or userinfo.get("preferred_username")

        return AccessToken(
            token=token,
            client_id="",  # Not available from userinfo
            scopes=["openid", "profile", "email"],  # TODO: Track actual scopes
            expires_at=int(time.time() + 3600),  # TODO: Get from JWT exp claim
            # Store username in scopes[0] as workaround for MCP SDK limitation
            # Or use custom AccessToken subclass with username field
        )

    async def _refresh_jwks(self):
        """Fetch JWKS from Nextcloud OIDC."""
        response = await self._client.get(self.jwks_uri)
        response.raise_for_status()
        self._jwks = response.json()
        self._jwks_expires = time.time() + 3600  # Cache for 1 hour

    async def close(self):
        """Cleanup resources."""
        await self._client.aclose()
```

#### 3. `nextcloud_mcp_server/auth/client_registration.py`
```python
async def register_client(
    nextcloud_url: str,
    client_name: str = "Nextcloud MCP Server",
    redirect_uris: list[str] = None
) -> dict:
    """Register MCP server as OAuth client with Nextcloud OIDC"""
    # POST to /apps/oidc/register
    # Return client_id, client_secret, expires_at

async def load_or_register_client(storage_path: str) -> dict:
    """Load existing client or register new one"""
    # Check storage file
    # Validate expiration
    # Re-register if expired
    # Persist credentials
```

#### 4. `nextcloud_mcp_server/auth/bearer_auth.py`
```python
class BearerAuth(httpx.Auth):
    """Bearer token authentication for httpx"""

    def __init__(self, token: str):
        self.token = token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request
```

### Modified Files

#### 1. `nextcloud_mcp_server/app.py`
```python
# Add OAuth configuration
from nextcloud_mcp_server.auth import NextcloudTokenVerifier, register_client

# In get_app():
if oauth_enabled:
    # Load or register client
    client_info = await load_or_register_client(storage_path)

    # Create token verifier
    token_verifier = NextcloudTokenVerifier(
        jwks_uri=f"{nextcloud_host}/apps/oidc/jwks",
        issuer=f"{nextcloud_host}"
    )

    # Configure FastMCP with OAuth
    mcp = FastMCP(
        "Nextcloud MCP",
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=nextcloud_host,
            resource_server_url=mcp_server_url,
            required_scopes=["openid", "profile"]
        ),
        lifespan=app_lifespan_oauth  # Don't create client in lifespan
    )
else:
    # Legacy BasicAuth mode
    mcp = FastMCP("Nextcloud MCP", lifespan=app_lifespan_basic)
```

#### 2. `nextcloud_mcp_server/client/__init__.py`
```python
class NextcloudClient:
    def __init__(self, base_url: str, username: str, auth: Auth | None = None):
        # Accept either BasicAuth or BearerAuth
        self._client = AsyncClient(base_url=base_url, auth=auth, ...)

    @classmethod
    def from_env(cls):
        """Legacy: Create from username/password env vars"""
        return cls(base_url, username, auth=BasicAuth(username, password))

    @classmethod
    def from_token(cls, base_url: str, token: str, username: str):
        """OAuth: Create from bearer token"""
        return cls(base_url, username, auth=BearerAuth(token))
```

#### 3. `nextcloud_mcp_server/server/notes.py` (and other tool modules)
```python
from nextcloud_mcp_server.auth import get_client_from_context

@mcp.tool()
async def nc_notes_create(title: str, content: str):
    ctx: Context = mcp.get_context()

    # OAuth mode: Get client from request context
    if oauth_enabled:
        client = get_client_from_context(ctx)
    else:
        # Legacy mode: Use lifespan client
        client = ctx.request_context.lifespan_context.client

    return await client.notes.create_note(...)
```

#### 4. `nextcloud_mcp_server/config.py`
```python
class NextcloudConfig:
    # Common
    host: str

    # OAuth mode
    oauth_enabled: bool = False
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    client_storage_path: str = ".nextcloud_oauth_client.json"
    mcp_server_url: str = "http://localhost:8000/mcp"
    required_scopes: list[str] = ["openid", "profile", "email"]

    # Legacy mode
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls):
        oauth_enabled = not (
            os.getenv("NEXTCLOUD_USERNAME") and
            os.getenv("NEXTCLOUD_PASSWORD")
        )
        return cls(oauth_enabled=oauth_enabled, ...)
```

### Configuration Files

#### Updated `env.sample`
```bash
# Nextcloud Instance
NEXTCLOUD_HOST=https://nextcloud.example.com

# ===== AUTHENTICATION MODE =====
# Choose ONE of the following:

# Option 1: OAuth2/OIDC (RECOMMENDED)
# - Requires Nextcloud OIDC app installed
# - Enable "Dynamic Client Registration" in OIDC app settings
# - Leave NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD empty
# - Optional: Pre-register client and provide credentials
NEXTCLOUD_OIDC_CLIENT_ID=
NEXTCLOUD_OIDC_CLIENT_SECRET=
NEXTCLOUD_OIDC_CLIENT_STORAGE=.nextcloud_oauth_client.json
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000/mcp

# Option 2: Basic Authentication (LEGACY - Will be deprecated)
# - Requires username and password
# - Less secure - credentials stored in environment
# - Use only for backward compatibility
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

## Dependencies

### New Python Dependencies
```toml
# pyproject.toml additions:
dependencies = [
    # ... existing ...
    "PyJWT[crypto]>=2.8.0",  # JWT validation
    "cryptography>=41.0.0",   # JWKS key handling (if not present)
]
```

## Nextcloud OIDC Setup

### Administrator Setup (One-time)
1. Install Nextcloud OIDC app from App Store
2. Navigate to Settings → OIDC
3. Enable "Dynamic Client Registration"
4. (Optional) Configure token expiration times via CLI:
   ```bash
   php occ config:app:set oidc expire_time --value "3600"
   php occ config:app:set oidc refresh_expire_time --value "86400"
   ```

### MCP Server Deployment (Zero-config)
1. Set `NEXTCLOUD_HOST` environment variable
2. Set `NEXTCLOUD_MCP_SERVER_URL` (if not localhost:8000)
3. Start MCP server → Auto-registers on first run
4. Client credentials stored in `.nextcloud_oauth_client.json`

### Alternative: Pre-registered Client
```bash
# Create client via CLI
php occ oidc:create \
  --name="Nextcloud MCP Server" \
  --type=confidential \
  --redirect-uri="http://localhost:8000/oauth/callback"

# Set credentials in environment
NEXTCLOUD_OIDC_CLIENT_ID=<generated-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<generated-secret>
```

## Testing Strategy

### Unit Tests
- Token validation with mocked JWKS
- JWT claim extraction
- Client registration flow
- Bearer auth implementation

### Integration Tests
- Dynamic client registration against test Nextcloud
- OAuth flow end-to-end
- Token-based API calls
- Client expiration and re-registration
- Dual-mode authentication (OAuth + BasicAuth)

### Test Fixtures
```python
# tests/conftest.py additions:
@pytest.fixture
def mock_oidc_server():
    """Mock Nextcloud OIDC endpoints"""
    # Mock /apps/oidc/openid-configuration
    # Mock /apps/oidc/jwks
    # Mock /apps/oidc/register
    # Mock /apps/oidc/token

@pytest.fixture
async def oauth_nc_client(mock_oidc_server):
    """NextcloudClient with OAuth token"""
    token = generate_test_jwt()
    return NextcloudClient.from_token(base_url, token, "testuser")
```

## Migration Path

### Phase 1: Implementation (Week 1-2)
- [ ] Implement token verifier with JWT validation
- [ ] Implement dynamic client registration
- [ ] Add BearerAuth for httpx
- [ ] Modify NextcloudClient for dual-mode auth
- [ ] Update app.py with OAuth configuration
- [ ] Add configuration management

### Phase 2: Testing (Week 2-3)
- [ ] Unit tests for all auth components
- [ ] Integration tests with test Nextcloud instance
- [ ] End-to-end OAuth flow testing
- [ ] Backward compatibility testing

### Phase 3: Documentation (Week 3)
- [ ] Update README.md with OAuth setup
- [ ] Update CLAUDE.md with architecture changes
- [ ] Add OAuth troubleshooting guide
- [ ] Document OIDC app configuration
- [ ] Add migration guide for existing deployments

### Phase 4: Deployment (Week 4)
- [ ] Release with both modes supported
- [ ] Monitor for issues
- [ ] Deprecation notice for BasicAuth
- [ ] Plan BasicAuth removal timeline (6+ months)

## Security Considerations

### Token Security
- Store client secrets securely (file permissions, secret managers)
- Validate JWT signatures against trusted JWKS
- Verify token claims (issuer, audience, expiration)
- Implement token refresh logic
- Rate limit token validation failures

### Client Registration Security
- Nextcloud OIDC provides BruteForce protection
- Dynamic clients limited to 100 per instance
- Clients expire after 1 hour (configurable)
- Admin must explicitly enable dynamic registration

### API Security
- Bearer tokens used for Nextcloud API calls
- Token scopes control access levels
- User context preserved in all API operations
- No credential storage in MCP server

## Performance Considerations

### JWT Validation Performance
- JWKS caching with TTL (e.g., 1 hour)
- Key rotation handling via JWKS refresh
- Local validation (no network call per request)
- Async validation to avoid blocking

### Client Creation
- OAuth mode: Per-request client creation (lightweight)
- BasicAuth mode: Single client in lifespan (current)
- Connection pooling maintained in both modes

## Future Enhancements

### Scope-based Authorization
- Define custom Nextcloud scopes for MCP operations
- Map MCP tools to required scopes
- Fine-grained permission control

### Multi-tenant Support
- Support multiple Nextcloud instances
- Per-user client registration
- Tenant isolation

### Token Introspection Fallback
- Implement RFC 7662 introspection
- Use if JWT validation fails
- Support for opaque tokens

### Admin Controls
- MCP server admin UI for OAuth config
- Client credential rotation
- Usage monitoring and logging

## Decisions Made (Post-Research)

1. **✅ Token Validation Method**: Userinfo endpoint (primary), JWT optional
   - Nextcloud OIDC does NOT provide introspection endpoint
   - Userinfo endpoint validates token AND returns user claims
   - JWT validation available as performance optimization if client configured

2. **✅ Client expiration handling**: Auto re-register with logging
   - Clients expire after 3600s by default
   - Check expiry on startup and periodically
   - Auto-register with backoff on failure

3. **✅ Scope requirements**: `["openid", "profile", "email"]`
   - Sufficient for basic user identification
   - Optional: Add `"roles"` or `"groups"` for group-based authorization

4. **✅ Token caching**: In-memory with 3600s TTL
   - Cache userinfo response (includes all needed claims)
   - Use token string as cache key
   - TTL matches default access token lifetime

5. **✅ Client storage**: JSON file with 0600 permissions
   - Default: `.nextcloud_oauth_client.json`
   - Configurable via env var
   - Contains: client_id, client_secret, issued_at

6. **✅ Username extraction**: From `sub` or `preferred_username` claim
   - Both contain Nextcloud username (verified)
   - Retrieved during token validation
   - Cached with token

7. **✅ BasicAuth deprecation**: 12 months after OAuth stable release
   - Phase 1: OAuth + BasicAuth (6 months)
   - Phase 2: OAuth only, deprecation warnings (6 months)
   - Phase 3: Remove BasicAuth

## Key Changes from Original Plan

### 1. Token Validation
**Original**: JWT validation with JWKS (primary), introspection (fallback)
**Updated**: Userinfo endpoint (primary), JWT validation (optional optimization)
- Reason: Nextcloud OIDC has no introspection endpoint

### 2. User Context Extraction
**Original**: Extract username from JWT claims
**Updated**: Fetch from userinfo endpoint during validation
- Reason: Opaque tokens by default, userinfo always works

### 3. Token Caching Strategy
**Original**: MCP SDK handles all caching
**Updated**: Custom cache in TokenVerifier for userinfo responses
- Reason: Need to cache username separately from AccessToken

### 4. JWT Support
**Original**: Required for all deployments
**Updated**: Optional performance optimization
- Reason: Requires per-client configuration in Nextcloud OIDC
- Default: Opaque tokens validated via userinfo

## References

- [MCP Python SDK OAuth Documentation](https://github.com/modelcontextprotocol/python-sdk)
- [MCP RFC 9728 Protected Resource Metadata](https://www.rfc-editor.org/rfc/rfc9728.html)
- [Nextcloud OIDC App Repository](https://github.com/H2CK/oidc)
- [OpenID Connect Dynamic Client Registration](https://openid.net/specs/openid-connect-registration-1_0.html)
- [RFC 9068 JWT Access Tokens](https://www.rfc-editor.org/rfc/rfc9068.html)
- [MCP Simple Auth Example](~/Software/python-sdk/examples/servers/simple-auth/)

## Success Criteria

✅ MCP server can authenticate via Nextcloud OIDC with zero manual client setup
✅ Dynamic client registration works automatically on first run
✅ JWT tokens validated locally without per-request network calls
✅ Backward compatibility maintained with BasicAuth mode
✅ All existing tests pass in both auth modes
✅ Documentation complete for OAuth setup and migration
✅ Security review passed (token handling, credential storage)
✅ Performance benchmarks meet targets (< 10ms token validation overhead)
