# ADR-005: Token Audience Validation and Security Compliance

**Status**: Accepted
**Date**: 2025-01-05
**Related**: Issue #261, ADR-004, upstream-oauth.md
**Supersedes**: Token passthrough mode in ADR-004

## Executive Summary

This ADR addresses a critical security vulnerability where the MCP server was passing tokens intended for itself directly to Nextcloud APIs (token passthrough). We will:

1. **Replace two non-compliant token verifiers** with a single `UnifiedTokenVerifier`
2. **Implement proper audience validation** requiring tokens to explicitly include appropriate audiences
3. **Support two compliant modes**:
   - **Multi-audience mode (default)**: Tokens contain both MCP and Nextcloud audiences
   - **Token exchange mode (opt-in)**: MCP tokens are exchanged for Nextcloud tokens via RFC 8693
4. **Remove all token passthrough paths** to comply with MCP security specification

The solution works within python-sdk constraints by implementing a two-layer architecture where token validation happens in the verifier and token exchange happens when creating API clients.

## Context

The MCP Security Best Practices specification explicitly forbids "token passthrough" - an anti-pattern where an MCP server accepts tokens from clients without validating they were properly issued to the MCP server, then passes them through to downstream APIs.

### Current Vulnerability

The Nextcloud MCP server currently supports two OAuth modes via the `ENABLE_TOKEN_EXCHANGE` flag:

1. **Pass-through mode** (`ENABLE_TOKEN_EXCHANGE=false`, **default**):
   - Accepts Flow 1 tokens with audience matching MCP server URL or client ID
   - Passes these tokens **directly** to Nextcloud APIs without audience transformation
   - **Violates MCP specification** - token intended for MCP server is used against Nextcloud

2. **Token exchange mode** (`ENABLE_TOKEN_EXCHANGE=true`, opt-in):
   - Accepts Flow 1 tokens with audience matching MCP server URL
   - Uses RFC 8693 to exchange for tokens with Nextcloud resource URI audience
   - **Compliant** with MCP specification but adds latency

**Location of vulnerability**: `nextcloud_mcp_server/context.py:62-66`

### Security Risks (per MCP specification)

1. **Security Control Circumvention**: Downstream APIs cannot distinguish between clients when all use the same token
2. **Accountability Issues**: Broken audit trails - logs show wrong identity/source
3. **Trust Boundary Violations**: Token meant for one service used for another
4. **Future Compatibility**: Cannot add security controls later without breaking changes

### OAuth Feature Status

The OAuth integration is currently **experimental** and requires an upstream fix in Nextcloud server to properly handle bearer tokens (see `docs/upstream-oauth.md` for details). Until the upstream fix is merged, **all breaking changes are acceptable** to ensure security compliance.

## Decision

We will **remove the token passthrough anti-pattern entirely** and enforce proper token audience validation in all OAuth deployments.

### Architectural Approach

Based on analysis of the existing code and python-sdk constraints, we will:

1. **Consolidate two non-compliant verifiers** (`NextcloudTokenVerifier` and `ProgressiveConsentTokenVerifier`) into a single `UnifiedTokenVerifier`
2. **Implement a two-layer architecture**:
   - **Verification Layer**: `UnifiedTokenVerifier` validates audiences only (complies with SDK protocol)
   - **Exchange Layer**: `context_helper.py` performs token exchange when needed
3. **Support two compliant modes** determined by the `ENABLE_TOKEN_EXCHANGE` setting:

### Mode 1: Multi-Audience Token Validation (Default)

Use multi-audience tokens directly. Per RFC 7519 Section 4.1.3, the MCP server validates only its own presence in the audience claim. Nextcloud independently validates its own audience when receiving API calls. This is the default mode when `ENABLE_TOKEN_EXCHANGE` is false or not set.

**Requirements**:
- Token must have `aud` claim containing:
  - **MCP server**: Client ID OR MCP server URL (e.g., `http://localhost:8000`)
- For Nextcloud API access to work, token should also include:
  - **Nextcloud**: Nextcloud resource URI (e.g., `http://localhost:8080`)
- Single token works for both MCP authentication and Nextcloud API access
- IdP must support multi-audience tokens for full functionality

**Resource URI Configuration**:
- Nextcloud OIDC app: Set via `default_resource_identifier` (default: `http://localhost:8080`)
- Keycloak: Configure resource servers with proper URIs
- MCP Server: Defaults to `NEXTCLOUD_MCP_SERVER_URL` environment variable

**Use case**: Standard deployments where IdP can issue tokens with multiple audiences

**Configuration**:
```bash
# Multi-audience mode (default when not set or false)
ENABLE_TOKEN_EXCHANGE=false  # or omit entirely

# Resource URIs for audience validation
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000  # MCP server URL (used as audience)
NEXTCLOUD_RESOURCE_URI=http://localhost:8080     # Nextcloud resource identifier

# Client ID (alternative audience for MCP)
OIDC_CLIENT_ID=nextcloud-mcp-server
```

**Token validation logic (RFC 7519 compliant)**:
```python
async def validate_token_audiences(token: dict, settings: Settings) -> bool:
    """
    Validate token has MCP audience per RFC 7519.

    Resource servers validate only their own presence in the audience claim.
    Nextcloud will independently validate its own audience when receiving API calls.
    This is NOT token passthrough (we validate the token). This IS token reuse
    which is allowed by RFC 8707 for multi-audience tokens between trusted services.
    """
    audiences = token.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]

    audiences_set = set(audiences)

    # MCP validates ONLY its own audience (client_id OR server_url OR server_url/mcp)
    mcp_valid = (
        settings.oidc_client_id in audiences_set or
        settings.nextcloud_mcp_server_url in audiences_set or
        f"{settings.nextcloud_mcp_server_url}/mcp" in audiences_set
    )

    if not mcp_valid:
        logger.error(
            f"Token rejected: Missing MCP audience. "
            f"Got {audiences}, need MCP ({settings.oidc_client_id} or "
            f"{settings.nextcloud_mcp_server_url})"
        )
        return False

    # Note: We do NOT validate Nextcloud's audience - that's Nextcloud's responsibility
    return True
```

### Mode 2: RFC 8693 Token Exchange (Opt-in)

Exchange MCP session tokens for Nextcloud-specific tokens via RFC 8693. This mode is activated when `ENABLE_TOKEN_EXCHANGE=true`.

**Requirements**:
- Client provides token with MCP audience (client ID or server URL)
- Server exchanges it for ephemeral token with Nextcloud resource URI
- IdP must support RFC 8693 token exchange endpoint
- Exchanged tokens cached for 5 minutes to reduce latency

**Performance Consideration**: In the context of an agentic LLM application, the additional network call for token exchange (typically 50-100ms) is negligible compared to LLM inference time (seconds). The security benefit far outweighs the minimal latency cost.

**Use case**:
- Deployments requiring strict audience separation
- IdPs with full RFC 8693 support (e.g., Keycloak with token exchange enabled)

**Configuration**:
```bash
# Token exchange mode (opt-in for strict separation)
ENABLE_TOKEN_EXCHANGE=true

# Resource URIs
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000  # MCP server URL
NEXTCLOUD_RESOURCE_URI=http://localhost:8080     # Nextcloud resource identifier

# Optional: Cache TTL
TOKEN_EXCHANGE_CACHE_TTL=300  # seconds (default: 300)

# OIDC discovery URL (token endpoint is auto-discovered from this)
OIDC_DISCOVERY_URL=http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration
```

**Token exchange with caching**:
```python
class TokenExchangeCache:
    """Cache exchanged tokens to reduce exchange frequency."""

    def __init__(self, ttl_seconds: int = 300):  # 5-minute default
        self._cache: dict[str, tuple[str, float]] = {}
        self._ttl = ttl_seconds

    async def get_or_exchange(
        self,
        subject_token: str,
        token_hash: str,
        exchange_func: Callable
    ) -> str:
        """Get cached token or perform exchange."""
        now = time.time()

        # Check cache
        if token_hash in self._cache:
            cached_token, expiry = self._cache[token_hash]
            if expiry > now:
                logger.debug(f"Using cached exchanged token (expires in {expiry - now:.1f}s)")
                return cached_token

        # Perform exchange
        logger.debug("Exchanging token for Nextcloud audience")
        nextcloud_token = await exchange_func(
            subject_token=subject_token,
            requested_audience=self.nextcloud_resource_uri
        )

        # Cache with TTL
        self._cache[token_hash] = (nextcloud_token, now + self._ttl)

        # Clean expired entries
        self._cache = {
            k: v for k, v in self._cache.items()
            if v[1] > now
        }

        return nextcloud_token
```

### Removed: Pass-through Mode (Non-compliant)

The pass-through mode is **removed immediately** as it violates MCP security requirements. No migration period is provided since the OAuth feature is experimental.

## Implementation

### 1. Environment Variables

**Required variables**:
```bash
# Resource URIs (required for audience validation)
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000   # MCP server URL (used as audience)
NEXTCLOUD_RESOURCE_URI=http://localhost:8080     # Nextcloud resource identifier

# Client identification
OIDC_CLIENT_ID=nextcloud-mcp-server             # Can also be valid audience for MCP
```

**Mode selection**:
```bash
# Multi-audience mode (default)
ENABLE_TOKEN_EXCHANGE=false                  # or omit entirely

# Token exchange mode (opt-in)
ENABLE_TOKEN_EXCHANGE=true                   # Activates RFC 8693 exchange
```

**Optional variables (exchange mode)**:
```bash
TOKEN_EXCHANGE_CACHE_TTL=300                 # Cache TTL in seconds (default: 300)
```

### 2. Consolidate Token Verifiers

**Current Issue**: Two TokenVerifier implementations exist (`NextcloudTokenVerifier` and `ProgressiveConsentTokenVerifier`), leading to code duplication, inconsistent validation logic, and pass-through vulnerabilities.

**Solution**: Consolidate into a single `UnifiedTokenVerifier` class that handles both compliant validation modes:

```python
class UnifiedTokenVerifier(TokenVerifier):
    """
    Unified token verifier supporting both multi-audience and token exchange modes.
    Compliant with MCP security specification - no token pass-through.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.mode = "exchange" if settings.enable_token_exchange else "multi-audience"

        # Common components
        self.http_client = httpx.AsyncClient(timeout=10.0)
        self.jwks_client = PyJWKClient(settings.jwks_uri) if settings.jwks_uri else None

        # Mode-specific initialization
        if self.mode == "exchange":
            # Exchange mode components (cache is in context helper, not here)
            self.introspection_uri = settings.introspection_uri
            self.client_secret = settings.oidc_client_secret

        logger.info(f"Token verifier initialized in {self.mode} mode")

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Verify token according to MCP TokenVerifier protocol.

        CRITICAL: This method only validates tokens - it does NOT perform exchange.
        Token exchange happens later in context_helper.py when creating NextcloudClient.

        Multi-audience mode: Validates token has BOTH MCP and Nextcloud audiences
        Exchange mode: Validates token has MCP audience ONLY (exchange happens later)
        """
        if self.mode == "multi-audience":
            return await self._verify_multi_audience_token(token)
        else:
            # Exchange mode: Only validate MCP audience here
            # Actual exchange happens in context_helper.py
            return await self._verify_mcp_audience_only(token)

    async def _verify_multi_audience_token(self, token: str) -> AccessToken | None:
        """
        Validate token has both MCP and Nextcloud audiences (Mode 1).
        Token can be used directly without exchange.
        """
        try:
            # Attempt JWT verification first
            if self._is_jwt_format(token) and self.jwks_client:
                payload = await self._verify_jwt_signature(token)
            else:
                # Fall back to introspection for opaque tokens
                payload = await self._introspect_token(token)
                if not payload:
                    return None

            # Validate both audiences are present
            if not self._validate_multi_audience(payload):
                logger.error(
                    f"Token rejected: Missing required audiences. "
                    f"Got {payload.get('aud')}, need both MCP and Nextcloud"
                )
                return None

            return self._create_access_token(token, payload)

        except Exception as e:
            logger.error(f"Multi-audience validation failed: {e}")
            return None

    async def _verify_mcp_audience_only(self, token: str) -> AccessToken | None:
        """
        Validate token has MCP audience only (Mode 2).
        Token will be exchanged later in context_helper.py.
        """
        try:
            # Attempt JWT verification first
            if self._is_jwt_format(token) and self.jwks_client:
                payload = await self._verify_jwt_signature(token)
            else:
                # Fall back to introspection for opaque tokens
                payload = await self._introspect_token(token)
                if not payload:
                    return None

            # Only validate MCP audience (exchange will handle Nextcloud)
            if not self._has_mcp_audience(payload):
                logger.error(
                    f"Token rejected: Missing MCP audience. "
                    f"Got {payload.get('aud')}, need {self.settings.oidc_client_id} "
                    f"or {self.settings.nextcloud_mcp_server_url}"
                )
                return None

            return self._create_access_token(token, payload)

        except Exception as e:
            logger.error(f"MCP audience validation failed: {e}")
            return None

    def _validate_multi_audience(self, payload: dict) -> bool:
        """Check if token has both MCP and Nextcloud audiences."""
        audiences = payload.get("aud", [])
        if isinstance(audiences, str):
            audiences = [audiences]

        audiences_set = set(audiences)

        # MCP must have at least one: client_id OR server_url
        mcp_valid = (
            self.settings.oidc_client_id in audiences_set or
            self.settings.nextcloud_mcp_server_url in audiences_set
        )

        # Nextcloud must have its resource URI
        nextcloud_valid = self.settings.nextcloud_resource_uri in audiences_set

        return mcp_valid and nextcloud_valid

    def _has_mcp_audience(self, payload: dict) -> bool:
        """Check if token has MCP audience (for exchange mode)."""
        audiences = payload.get("aud", [])
        if isinstance(audiences, str):
            audiences = [audiences]

        audiences_set = set(audiences)
        return (
            self.settings.oidc_client_id in audiences_set or
            self.settings.nextcloud_mcp_server_url in audiences_set
        )
```

**Key Design Decisions**:

1. **Separation of Concerns**: The verifier ONLY validates tokens. Token exchange happens in `context_helper.py` when creating the NextcloudClient, not in the verifier itself.

2. **Python SDK Compatibility**: The MCP python-sdk's `TokenVerifier` protocol requires returning an `AccessToken` object. We comply with this interface while deferring exchange to the context layer.

3. **Mode Selection**: Single class with mode-based behavior selected at startup via `ENABLE_TOKEN_EXCHANGE` environment variable.

**Benefits**:
- Single source of truth for token validation logic
- Clear separation between validation and exchange
- Compliant with MCP TokenVerifier protocol
- Eliminates token pass-through vulnerability
- Consistent error handling across all modes

### 3. Error Handling and Propagation

Token validation errors will be handled consistently:

```python
class TokenValidationError(Exception):
    """Raised when token validation fails."""

    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.details = details or {}
        self.http_status = 401  # Unauthorized

async def _verify_jwt_token(self, token: str) -> AccessToken:
    """Verify JWT token with proper audience validation."""
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError as e:
        raise TokenValidationError(
            "Invalid JWT token format",
            details={"error": str(e)}
        )

    # Validate audiences
    if not await self.validate_token_audiences(payload, self.settings):
        raise TokenValidationError(
            "Token audiences do not meet requirements",
            details={
                "got": payload.get("aud"),
                "need_mcp": [self.settings.oidc_client_id, self.settings.mcp_resource_uri],
                "need_nextcloud": self.settings.nextcloud_resource_uri
            }
        )

    # Additional validation (expiry, issuer, etc.)
    # ...

    return AccessToken(...)
```

### 4. Configuration Validation

Startup validation ensures consistent configuration:

```python
def validate_oauth_configuration(settings: Settings):
    """Validate OAuth configuration at startup."""
    if not settings.nextcloud_mcp_server_url:
        raise ValueError("NEXTCLOUD_MCP_SERVER_URL is required for audience validation")

    if not settings.nextcloud_resource_uri:
        raise ValueError("NEXTCLOUD_RESOURCE_URI is required for audience validation")

    if settings.enable_token_exchange:
        logger.info("Token exchange mode enabled - using RFC 8693 for strict audience separation")
        if not settings.oidc_discovery_url:
            logger.warning(
                "No OIDC_DISCOVERY_URL configured. "
                "Token endpoint discovery may fail."
            )
    else:
        logger.info("Multi-audience mode enabled - tokens must contain both MCP and Nextcloud audiences")
```

### 5. Context Helper Updates

Update `context.py` to handle token exchange at the NextcloudClient creation point:

```python
async def get_client(ctx: Context) -> NextcloudClient:
    """Get NextcloudClient based on authentication mode."""
    settings = get_settings()
    lifespan_ctx = ctx.request_context.lifespan_context

    # BasicAuth mode - unchanged
    if hasattr(lifespan_ctx, "client"):
        return lifespan_ctx.client

    # OAuth mode
    if hasattr(lifespan_ctx, "nextcloud_host"):
        if settings.enable_token_exchange:
            # Mode 2: Exchange MCP token for Nextcloud token
            logger.debug("Token exchange mode - exchanging token")
            return await get_session_client_from_context(
                ctx, lifespan_ctx.nextcloud_host
            )
        else:
            # Mode 1: Token already has both audiences, use directly
            logger.debug("Multi-audience mode - using token directly")
            return get_client_from_context(ctx, lifespan_ctx.nextcloud_host)

    raise AttributeError("Unknown context type")


# In context_helper.py
async def get_session_client_from_context(
    ctx: Context, base_url: str
) -> NextcloudClient:
    """
    Create NextcloudClient using RFC 8693 token exchange.

    CRITICAL: This is where token exchange happens, NOT in the verifier.
    The verifier already validated the MCP audience; now we exchange for Nextcloud.
    """
    # Extract validated MCP token from context
    access_token: AccessToken = ctx.request_context.request.user.access_token
    mcp_token = access_token.token
    username = access_token.resource  # Username from verifier

    # Check cache for existing exchanged token
    cache_key = hashlib.sha256(mcp_token.encode()).hexdigest()
    if cache_key in _exchange_cache:
        cached_token, expiry = _exchange_cache[cache_key]
        if time.time() < expiry:
            logger.debug("Using cached exchanged token")
            return NextcloudClient.from_token(
                base_url=base_url, token=cached_token, username=username
            )

    # Perform RFC 8693 token exchange
    logger.info("Exchanging MCP token for Nextcloud API token")
    exchanged_token, expires_in = await exchange_token_for_audience(
        subject_token=mcp_token,
        requested_audience=settings.nextcloud_resource_uri,
        requested_scopes=None,  # Nextcloud doesn't enforce scopes
    )

    # Cache the exchanged token
    _exchange_cache[cache_key] = (
        exchanged_token,
        time.time() + min(expires_in, settings.token_exchange_cache_ttl)
    )

    # Create client with exchanged token
    return NextcloudClient.from_token(
        base_url=base_url, token=exchanged_token, username=username
    )


def get_client_from_context(ctx: Context, base_url: str) -> NextcloudClient:
    """
    Create NextcloudClient for multi-audience mode (no exchange needed).
    Token already contains both MCP and Nextcloud audiences.
    """
    access_token: AccessToken = ctx.request_context.request.user.access_token

    # Token was already validated to have both audiences
    # Can use directly without exchange
    return NextcloudClient.from_token(
        base_url=base_url,
        token=access_token.token,
        username=access_token.resource  # Username from verifier
    )
```

**Key Implementation Details**:

1. **Token Exchange Location**: Exchange happens in `get_session_client_from_context()`, not in the verifier
2. **Caching**: Exchange cache is maintained in the context helper to prevent repeated exchanges
3. **Python SDK Integration**: We work with the SDK's `AccessToken` object and create `NextcloudClient` with the appropriate token

### 6. Performance Benchmarks

Expected performance characteristics:

| Mode | Latency Impact | Use Case |
|------|---------------|----------|
| Multi-Audience | 0ms (no extra calls) | Default, best performance |
| Token Exchange (cached) | ~1ms (cache lookup) | Recently used tokens |
| Token Exchange (fresh) | 50-100ms (network call) | First use or after cache expiry |

In context of LLM operations:
- LLM inference: 2-10 seconds typical
- Token exchange: 0.05-0.1 seconds (1-2% of total request time)
- **Conclusion**: Performance impact is negligible

### 7. IdP Configuration Examples

#### Nextcloud Built-in OIDC (Multi-Audience)
```bash
# Set resource identifier for Nextcloud
php occ config:app:set oidc default_resource_identifier --value="http://localhost:8080"

# MCP server configuration (multi-audience mode)
ENABLE_TOKEN_EXCHANGE=false  # or omit
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000
NEXTCLOUD_RESOURCE_URI=http://localhost:8080
```

#### Keycloak with Multi-Audience
```bash
# 1. Create resource servers in Keycloak
# Admin Console > Clients > Create Client
# - MCP Resource Server: http://localhost:8000
# - Nextcloud Resource Server: http://localhost:8080

# 2. Configure token mapper for multi-audience
# Client > Mappers > Create
# - Mapper Type: Audience
# - Included Client Audience: Select both resource servers

# 3. MCP server configuration
ENABLE_TOKEN_EXCHANGE=false  # Multi-audience mode
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000
NEXTCLOUD_RESOURCE_URI=http://localhost:8080
OIDC_DISCOVERY_URL=http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration
```

#### Keycloak with Token Exchange
```bash
# 1. Enable token exchange in Keycloak
# Realm Settings > Client Policies > Add permission for token-exchange

# 2. MCP server configuration
ENABLE_TOKEN_EXCHANGE=true  # Exchange mode
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000
NEXTCLOUD_RESOURCE_URI=http://localhost:8080
OIDC_DISCOVERY_URL=http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration
# Note: Token endpoint is auto-discovered from the OIDC discovery URL
```

## Testing

### Unit Tests
```python
@pytest.mark.unit
async def test_multi_audience_validation():
    """Test multi-audience token validation logic."""
    validator = UnifiedTokenVerifier(
        nextcloud_mcp_server_url="http://localhost:8000",
        nextcloud_resource_uri="http://localhost:8080",
        oidc_client_id="test-client"
    )

    # Valid: Both resource URIs
    token = {"aud": ["http://localhost:8000", "http://localhost:8080"]}
    assert await validator.validate_token_audiences(token)

    # Valid: Client ID + Nextcloud URI
    token = {"aud": ["test-client", "http://localhost:8080"]}
    assert await validator.validate_token_audiences(token)

    # Invalid: Missing Nextcloud
    token = {"aud": ["http://localhost:8000"]}
    assert not await validator.validate_token_audiences(token)

    # Invalid: Missing MCP
    token = {"aud": ["http://localhost:8080"]}
    assert not await validator.validate_token_audiences(token)

@pytest.mark.unit
async def test_token_exchange_caching():
    """Test token exchange caching behavior."""
    cache = TokenExchangeCache(ttl_seconds=5)
    exchange_count = 0

    async def mock_exchange(subject_token: str, requested_audience: str):
        nonlocal exchange_count
        exchange_count += 1
        return f"exchanged-{exchange_count}"

    # First call - should exchange
    token1 = await cache.get_or_exchange("subject-1", "hash-1", mock_exchange)
    assert token1 == "exchanged-1"
    assert exchange_count == 1

    # Second call with same hash - should use cache
    token2 = await cache.get_or_exchange("subject-1", "hash-1", mock_exchange)
    assert token2 == "exchanged-1"
    assert exchange_count == 1  # No new exchange

    # Different hash - should exchange
    token3 = await cache.get_or_exchange("subject-2", "hash-2", mock_exchange)
    assert token3 == "exchanged-2"
    assert exchange_count == 2
```

### Integration Tests
```python
@pytest.mark.integration
async def test_multi_audience_e2e(nc_mcp_oauth_client):
    """Test end-to-end multi-audience token flow."""
    # Token should have both audiences
    result = await nc_mcp_oauth_client.call_tool("nc_notes_list_notes")
    assert result.success

    # Verify token was not exchanged (check logs)
    logs = await get_server_logs()
    assert "Token exchange" not in logs
    assert "Multi-audience validation passed" in logs

@pytest.mark.integration
async def test_token_exchange_e2e(nc_mcp_keycloak_client):
    """Test end-to-end token exchange flow."""
    # Start with MCP-only token
    result = await nc_mcp_keycloak_client.call_tool("nc_notes_list_notes")
    assert result.success

    # Verify exchange happened
    logs = await get_server_logs()
    assert "Exchanging token for Nextcloud audience" in logs

    # Second call should use cache
    result2 = await nc_mcp_keycloak_client.call_tool("nc_notes_list_notes")
    assert result2.success

    logs2 = await get_server_logs()
    assert "Using cached exchanged token" in logs2

@pytest.mark.integration
async def test_invalid_audience_rejection(nc_mcp_oauth_client):
    """Test that invalid audiences are rejected with clear errors."""
    # Manually inject token with wrong audience
    invalid_token = create_test_token(aud=["wrong-audience"])

    with pytest.raises(TokenValidationError) as exc_info:
        await nc_mcp_oauth_client.call_tool(
            "nc_notes_list_notes",
            token=invalid_token
        )

    assert exc_info.value.http_status == 401
    assert "Token audiences do not meet requirements" in str(exc_info.value)
    assert exc_info.value.details["need_nextcloud"] == "http://localhost:8080"
```

### Load Tests
```python
@pytest.mark.load
async def test_token_validation_performance():
    """Benchmark token validation overhead."""
    # Test both modes under load
    results = {}

    for enable_exchange in [False, True]:
        os.environ["ENABLE_TOKEN_EXCHANGE"] = str(enable_exchange).lower()
        mode = "exchange" if enable_exchange else "multi-audience"

        start = time.time()
        await run_concurrent_requests(
            num_workers=50,
            requests_per_worker=100,
            operation="nc_notes_list_notes"
        )
        duration = time.time() - start

        results[mode] = {
            "total_time": duration,
            "requests_per_second": 5000 / duration,
            "avg_latency_ms": (duration / 5000) * 1000
        }

    # Multi-audience should be faster (no exchange)
    assert results["multi-audience"]["avg_latency_ms"] < results["exchange"]["avg_latency_ms"]

    # But both should be acceptable for LLM context
    assert results["exchange"]["avg_latency_ms"] < 200  # Max 200ms overhead
```

## Troubleshooting

### Common Issues and Solutions

1. **"Token audiences do not meet requirements"**
   - Check token with jwt.io to see actual audiences
   - Verify `NEXTCLOUD_MCP_SERVER_URL` and `NEXTCLOUD_RESOURCE_URI` match IdP configuration
   - For Nextcloud OIDC: Check `occ config:app:get oidc default_resource_identifier`

2. **"Token exchange failed"**
   - Verify IdP supports RFC 8693 token exchange
   - Check that OIDC discovery URL is correctly configured
   - Verify token endpoint is accessible from the MCP server
   - Enable debug logging: `LOG_LEVEL=DEBUG`

3. **"Configuration validation failed at startup"**
   - Ensure `ENABLE_TOKEN_EXCHANGE` is set correctly (true for exchange mode, false/omit for multi-audience)
   - Both resource URIs must be configured (`NEXTCLOUD_MCP_SERVER_URL` and `NEXTCLOUD_RESOURCE_URI`)
   - Check that IdP is configured to issue tokens with appropriate audiences

4. **Performance issues with exchange mode**
   - Check cache hit rate in logs
   - Increase `TOKEN_EXCHANGE_CACHE_TTL` if tokens are long-lived
   - Consider switching to multi-audience mode if IdP supports it

### Debug Commands

```bash
# Check current token audiences (requires jq)
echo $ACCESS_TOKEN | cut -d. -f2 | base64 -d | jq '.aud'

# Test multi-audience validation
curl -X POST http://localhost:8000/mcp/v1/tools/nc_notes_list_notes \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"

# Check server logs for validation details
docker compose logs mcp-oauth | grep -E "(audience|validation|exchange)"

# Verify IdP resource configuration (Keycloak)
curl http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration | jq '.resource_servers'
```

## Security Considerations

### Threat Model

**Threat**: Malicious client uses stolen MCP token against Nextcloud directly
- **Mitigation**: Tokens must contain correct resource URI audiences
- **Multi-Audience**: Requires token with both audiences (harder to obtain)
- **Exchange**: MCP token cannot be used directly against Nextcloud

**Threat**: Token reuse across services
- **Mitigation**: Strict audience validation ensures tokens only work for intended services
- **Validation**: Both MCP and Nextcloud validate their respective audiences

**Threat**: Audit trail confusion
- **Mitigation**: Clear separation of token contexts
- **Multi-Audience**: Different audience claims identify service context
- **Exchange**: Completely different tokens for each service

### Compliance

This implementation ensures **full compliance** with:
- [MCP Security Best Practices - Token Passthrough](https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices#token-passthrough)
- OAuth 2.0 Resource Indicators (RFC 8707)
- OAuth 2.0 Token Exchange (RFC 8693)

## Migration Guide

### For Existing Deployments

**BREAKING CHANGE**: All OAuth deployments must be reconfigured to comply with the new audience validation requirements.

#### Step 1: Update Environment Variables

Add the required resource URI configuration:

```bash
# Required for all OAuth modes
NEXTCLOUD_MCP_SERVER_URL=http://your-mcp-server:8000  # Your MCP server URL
NEXTCLOUD_RESOURCE_URI=http://your-nextcloud:8080      # Your Nextcloud instance URL
```

#### Step 2: Choose Your Mode

**Option A: Multi-Audience Mode (Recommended for most deployments)**
```bash
ENABLE_TOKEN_EXCHANGE=false  # or omit entirely
```

Configure your IdP to issue tokens with both audiences:
- MCP audience: Your client ID or MCP server URL
- Nextcloud audience: Your Nextcloud resource URI

**Option B: Token Exchange Mode (For strict separation)**
```bash
ENABLE_TOKEN_EXCHANGE=true
TOKEN_EXCHANGE_CACHE_TTL=300  # Optional, default is 300 seconds
```

Configure your IdP to:
- Issue tokens with MCP audience only
- Support RFC 8693 token exchange

#### Step 3: Update IdP Configuration

**For Nextcloud OIDC**:
```bash
# Set the resource identifier
docker compose exec app php occ config:app:set oidc default_resource_identifier --value="http://your-nextcloud:8080"
```

**For Keycloak**:
1. Create resource servers for both MCP and Nextcloud
2. Configure audience mappers appropriately
3. Enable token exchange if using exchange mode

#### Step 4: Test Your Configuration

```bash
# Test multi-audience validation
curl -X POST http://localhost:8000/mcp/v1/tools/nc_notes_list_notes \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"

# Check logs for validation details
docker compose logs mcp-oauth | grep -E "(audience|validation)"
```

### Code Migration

If you have custom code using the old verifiers:

**Before**:
```python
from nextcloud_mcp_server.auth.token_verifier import NextcloudTokenVerifier
verifier = NextcloudTokenVerifier(...)
```

**After**:
```python
from nextcloud_mcp_server.auth.unified_verifier import UnifiedTokenVerifier
verifier = UnifiedTokenVerifier(settings)
```

## Consequences

### Positive

1. **Security Compliance**: Eliminates token passthrough vulnerability
2. **OAuth Spec Compliance**: Follows RFC 7519 Section 4.1.3 - resource servers validate only their own audience
3. **Clear Architecture**: Explicit validation modes with resource URI semantics
4. **Performance**: Negligible impact in LLM context (1-2% of request time)
5. **Flexibility**: Supports both simple (multi-audience) and strict (exchange) modes
6. **Audit Trail**: Proper audience separation enables accurate logging
7. **Simpler Logic**: Each resource server independently validates its own audience, reducing complexity

### Negative

1. **Breaking Change**: Existing deployments must reconfigure
2. **Configuration Required**: Must specify resource URIs explicitly
3. **IdP Requirements**: Requires proper resource server configuration

### Neutral

1. **Experimental Status**: Breaking changes acceptable until upstream fix merged
2. **Performance Trade-off**: Security benefit outweighs minimal latency cost

## References

- [Issue #261: Avoid Token Passthrough in OAuth flow](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/261)
- [MCP Security Best Practices](https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices)
- [RFC 8693: OAuth 2.0 Token Exchange](https://datatracker.ietf.org/doc/html/rfc8693)
- [RFC 8707: Resource Indicators for OAuth 2.0](https://datatracker.ietf.org/doc/html/rfc8707)
- [ADR-004: Federated Authentication Architecture](./ADR-004-mcp-application-oauth.md)
- [Upstream OAuth Requirements](./upstream-oauth.md)

## Python SDK Constraints and Architecture

### SDK TokenVerifier Protocol

The MCP python-sdk defines a strict `TokenVerifier` protocol that our implementation must follow:

```python
class TokenVerifier(Protocol):
    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token and return access info if valid."""
```

**Key Constraints**:

1. **Single Method Interface**: The verifier can only validate tokens, not modify or exchange them
2. **Return Type**: Must return an `AccessToken` object or `None`
3. **Token Access**: The original bearer token is passed through the SDK to API calls unless we intervene at a different layer

### Architecture Decisions

Given these constraints, we implement a **two-layer architecture**:

1. **Token Verifier Layer** (`UnifiedTokenVerifier`):
   - Validates token audiences according to configured mode
   - Returns `AccessToken` objects to satisfy SDK protocol
   - Does NOT perform token exchange

2. **Context Helper Layer** (`context_helper.py`):
   - Extracts tokens from MCP context
   - Performs RFC 8693 token exchange when needed
   - Creates `NextcloudClient` with appropriate token
   - Maintains exchange cache to minimize latency

This separation ensures:
- Compliance with MCP SDK protocol
- Clean separation of concerns
- Token exchange happens only when creating API clients
- Pass-through vulnerability is eliminated

## Implementation Checklist

- [ ] Create `UnifiedTokenVerifier` class replacing both existing verifiers
- [ ] Remove pass-through mode from `context.py` entirely
- [ ] Update `context_helper.py` to implement token exchange with caching
- [ ] Implement multi-audience validation in unified verifier
- [ ] Implement MCP-only validation for exchange mode in unified verifier
- [ ] Add token exchange caching mechanism in context helper layer
- [ ] Update docker-compose.yml with resource URI configuration:
  - `NEXTCLOUD_MCP_SERVER_URL` (required)
  - `NEXTCLOUD_RESOURCE_URI` (required)
  - `TOKEN_EXCHANGE_CACHE_TTL` (optional, default: 300)
- [ ] Configure Nextcloud OIDC `default_resource_identifier`
- [ ] Configure Keycloak resource servers with proper audiences
- [ ] Remove `NextcloudTokenVerifier` class
- [ ] Remove `ProgressiveConsentTokenVerifier` class
- [ ] Write unit tests for unified verifier (both modes)
- [ ] Write integration tests for token exchange flow
- [ ] Update documentation with IdP configuration guides
- [ ] Add performance benchmarks to CI pipeline
- [ ] Update CHANGELOG.md with breaking changes notice