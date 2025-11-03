# Token Acquisition Patterns for ADR-004 Progressive Consent

## Overview

ADR-004 Progressive Consent establishes the authorization architecture (Flow 1 for client auth, Flow 2 for resource provisioning). This document describes **how tokens are acquired for different operational contexts** within that architecture.

**Key Principle**: Refresh tokens from Flow 2 (Progressive Consent) should **NEVER** be used for MCP tool calls - they are exclusively for background jobs.

## Implementation Status

**Current Status**: ✅ Token exchange infrastructure implemented, available as opt-in feature

The MCP server supports two token acquisition modes:
1. **Pass-through mode** (default, `ENABLE_TOKEN_EXCHANGE=false`): Simple, stateless
2. **Token exchange mode** (opt-in, `ENABLE_TOKEN_EXCHANGE=true`): Enhanced security with token delegation

Both modes maintain the critical separation: **refresh tokens are never used for tool calls**.

## Current Default (Pass-Through Mode)

### What Happens (ENABLE_TOKEN_EXCHANGE=false):
1. Client gets Flow 1 token (`aud: "mcp-server"`)
2. Client calls MCP tool
3. Server validates Flow 1 token
4. Server passes Flow 1 token to Nextcloud
5. Nextcloud validates token with IdP
6. Refresh tokens (from Flow 2) used **only** for background jobs

### Characteristics:
- ✅ Simple, stateless operation
- ✅ Clear separation: Flow 1 tokens for sessions, refresh tokens for background
- ✅ Lower latency (no token exchange round-trip)
- ✅ Works with any OAuth IdP

## Optional Token Exchange Mode

### Token Exchange Pattern (ENABLE_TOKEN_EXCHANGE=true)

**MCP Session (Foreground Operations)**:

```
┌─────────────┐     Flow 1 Token      ┌──────────────┐
│  MCP Client │ ───(aud: mcp-server)──> │  MCP Server  │
└─────────────┘                        └──────────────┘
                                              │
                    Tool Call                 │
                    "search_notes()"          │
                                              ▼
                                    ┌─────────────────────┐
                                    │ Token Exchange      │
                                    │ 1. Validate Flow 1  │
                                    │ 2. Check permission │
                                    │ 3. Request delegated│
                                    │    Nextcloud token  │
                                    └─────────────────────┘
                                              │
                                              │ Exchange Request
                                              ▼
                                    ┌─────────────────────┐
                                    │ IdP Token Endpoint  │
                                    │ (Token Exchange)    │
                                    └─────────────────────┘
                                              │
                                              │ Delegated Token
                                              │ (aud: nextcloud)
                                              │ (limited scopes)
                                              │ (short-lived)
                                              ▼
                                    ┌─────────────────────┐
                                    │ Nextcloud API Call  │
                                    │ GET /notes          │
                                    └─────────────────────┘
```

**Key Properties of Session Tokens:**
- ✅ Generated **on-demand** during tool execution
- ✅ **Ephemeral** - used only for current operation
- ✅ **NOT stored** - discarded after use
- ✅ **Limited scopes** - only what tool needs (e.g., `notes:read` for search)
- ✅ **Short-lived** - expires quickly (e.g., 5 minutes)

**Background Jobs (Offline Operations)**:

```
┌─────────────────┐     Scheduled Job      ┌──────────────┐
│ Background      │ ──────────────────────> │  Worker      │
│ Scheduler       │                         │  Process     │
└─────────────────┘                         └──────────────┘
                                                    │
                                                    │ Use stored
                                                    │ refresh token
                                                    ▼
                                          ┌─────────────────────┐
                                          │ Refresh Token Store │
                                          │ (Flow 2 provisioned)│
                                          └─────────────────────┘
                                                    │
                                                    │ Refresh Token
                                                    ▼
                                          ┌─────────────────────┐
                                          │ IdP Token Endpoint  │
                                          │ (Refresh Grant)     │
                                          └─────────────────────┘
                                                    │
                                                    │ Background Token
                                                    │ (aud: nextcloud)
                                                    │ (different scopes)
                                                    │ (longer-lived)
                                                    ▼
                                          ┌─────────────────────┐
                                          │ Nextcloud API       │
                                          │ (Background Sync)   │
                                          └─────────────────────┘
```

**Key Properties of Background Tokens:**
- ✅ Obtained from **stored refresh token** (Flow 2)
- ✅ **Different scopes** than session tokens (e.g., `notes:sync`, `files:sync`)
- ✅ **Longer-lived** for background operations
- ✅ **Never used for MCP sessions**
- ✅ **Only for offline/background jobs**

## Implementation Requirements

### 1. Token Exchange Endpoint

Implement RFC 8693 Token Exchange:

```python
# nextcloud_mcp_server/auth/token_exchange.py

async def exchange_token_for_delegation(
    flow1_token: str,
    requested_audience: str = "nextcloud",
    requested_scopes: list[str] | None = None
) -> tuple[str, int]:
    """
    Exchange Flow 1 MCP token for delegated Nextcloud token.

    This implements RFC 8693 Token Exchange for on-behalf-of delegation.

    IMPORTANT: Nextcloud doesn't support OAuth scopes natively. Scopes are
    soft-scopes enforced by the MCP server via @require_scopes decorator,
    not by the IdP or Nextcloud. Therefore, requested_scopes are not passed
    to the IdP during token exchange.

    Args:
        flow1_token: The MCP session token (aud: "mcp-server")
        requested_audience: Target audience (usually "nextcloud")
        requested_scopes: Ignored (Nextcloud doesn't support scopes)

    Returns:
        Tuple of (delegated_token, expires_in)
    """
    # 1. Validate Flow 1 token (audience check)
    # 2. Check user has provisioned Nextcloud access (Flow 2)
    # 3. Request token exchange from IdP (without scopes - Nextcloud doesn't support them)
    # 4. Return ephemeral delegated token
```

### 2. Unified get_client() Pattern

The token acquisition mode is handled transparently by `get_client()`:

```python
# nextcloud_mcp_server/context.py

async def get_client(ctx: Context) -> NextcloudClient:
    """
    Get the appropriate Nextcloud client based on authentication mode.

    This function handles three modes:
    1. BasicAuth mode: Returns shared client from lifespan context
    2. OAuth pass-through mode (ENABLE_TOKEN_EXCHANGE=false, default):
       Verifies Flow 1 token and passes it to Nextcloud
    3. OAuth token exchange mode (ENABLE_TOKEN_EXCHANGE=true):
       Exchanges Flow 1 token for ephemeral Nextcloud token via RFC 8693
    """
    settings = get_settings()
    lifespan_ctx = ctx.request_context.lifespan_context

    # BasicAuth mode - use shared client (no token exchange)
    if hasattr(lifespan_ctx, "client"):
        return lifespan_ctx.client

    # OAuth mode (has 'nextcloud_host' attribute)
    if hasattr(lifespan_ctx, "nextcloud_host"):
        # Check if token exchange is enabled
        if settings.enable_token_exchange:
            # Token exchange mode: Exchange Flow 1 token for ephemeral Nextcloud token
            return await get_session_client_from_context(
                ctx, lifespan_ctx.nextcloud_host
            )
        else:
            # Pass-through mode (default): Verify and pass Flow 1 token to Nextcloud
            return get_client_from_context(ctx, lifespan_ctx.nextcloud_host)
```

### 3. MCP Tool Pattern (No Changes Required!)

Tools use the same pattern regardless of token acquisition mode:

```python
@mcp.tool()
@require_scopes("notes:read")  # Soft-scope enforced by MCP server, not Nextcloud
@require_provisioning
async def nc_notes_search_notes(query: str, ctx: Context) -> SearchNotesResponse:
    """Search notes by title or content."""

    # get_client() handles both pass-through and token exchange modes
    client = await get_client(ctx)

    # Execute operation
    results = await client.notes.search_notes(query=query)

    # In token exchange mode, ephemeral token is automatically discarded
    # In pass-through mode, Flow 1 token was validated and passed through
    return SearchNotesResponse(results=results)
```

**Key Benefit**: Tools don't need to know which mode is active. The token acquisition pattern is configured at the server level via `ENABLE_TOKEN_EXCHANGE`.

### 4. Background Job Pattern

Background jobs use a **different token acquisition pattern** - they use refresh tokens from Flow 2:

```python
# Background worker
async def sync_notes_job(user_id: str):
    """Background job to sync notes."""

    # Get refresh token stored during Flow 2 (Progressive Consent)
    token_storage = get_token_storage()
    refresh_token = await token_storage.get_refresh_token(user_id)

    if not refresh_token:
        logger.warning(f"No refresh token for user {user_id}")
        return

    # Use refresh token to get Nextcloud access token
    idp_client = get_idp_client()
    response = await idp_client.refresh_token(
        refresh_token=refresh_token,
        audience='nextcloud'
    )

    # Create client with background token (can be cached)
    client = NextcloudClient.from_token(
        base_url=NEXTCLOUD_HOST,
        token=response.access_token,
        username=user_id
    )

    # Perform background sync
    await client.notes.sync_all()
```

**Key differences from tool calls:**
- Uses refresh tokens from Flow 2 (Progressive Consent provisioning)
- Tokens can be cached for efficiency (longer-lived operations)
- No user interaction possible (offline)
- Never triggered during MCP tool execution

## Security Benefits

### Proper Token Exchange:
1. ✅ **Least Privilege**: Each operation gets only needed scopes
2. ✅ **Time-Limited**: Session tokens expire quickly
3. ✅ **Audit Trail**: Each exchange can be logged
4. ✅ **Token Isolation**: Session ≠ Background tokens
5. ✅ **Revocation**: Can revoke background access without affecting active sessions

### Current Incorrect Pattern:
1. ❌ **Over-Privileged**: Refresh token has all scopes
2. ❌ **Long-Lived**: Same token reused indefinitely
3. ❌ **No Separation**: Sessions and background jobs use same credential
4. ❌ **Revocation Issues**: Revoking affects everything

## Implementation Steps

### Phase 1: Token Exchange (High Priority)
1. Implement RFC 8693 token exchange endpoint
2. Update Token Broker with `get_session_token()` vs `get_background_token()`
3. Modify tool pattern to use token exchange

### Phase 2: Scope Separation (High Priority)
1. Define session scopes vs background scopes
2. Update provisioning flow to request appropriate scopes
3. Validate scopes in token exchange

### Phase 3: Background Jobs (Medium Priority)
1. Implement background worker pattern
2. Create scheduled jobs (note sync, etc.)
3. Use background token pattern

### Phase 4: Testing (High Priority)
1. Test token exchange flow end-to-end
2. Verify session tokens are ephemeral
3. Verify background tokens are separate
4. Load test token exchange performance

## References

- **RFC 8693**: OAuth 2.0 Token Exchange
- **RFC 9068**: JSON Web Token (JWT) Profile for OAuth 2.0 Access Tokens
- **ADR-004**: Progressive Consent OAuth Flows
- **OAuth 2.0 Delegation**: On-Behalf-Of vs Impersonation patterns

## Status

**Current Status**: ✅ Token exchange infrastructure implemented, available as opt-in feature
**Modes Available**:
- ✅ Pass-through mode (default, `ENABLE_TOKEN_EXCHANGE=false`): Simple, stateless
- ✅ Token exchange mode (opt-in, `ENABLE_TOKEN_EXCHANGE=true`): Enhanced security

**Implementation Complete**:
- ✅ `token_exchange.py` module with RFC 8693 support
- ✅ Fallback to refresh grant when RFC 8693 not supported
- ✅ `get_client()` unified pattern (handles both modes transparently)
- ✅ Tokens never cached in token exchange mode (ephemeral)
- ✅ Background jobs use separate pattern (refresh tokens from Flow 2)

## Configuration

To enable token exchange mode:

```bash
# docker-compose.yml or .env
ENABLE_TOKEN_EXCHANGE=true
```

When enabled, all MCP tool calls will use token exchange (RFC 8693) to obtain ephemeral Nextcloud tokens. When disabled (default), Flow 1 tokens are passed through to Nextcloud.

## Nextcloud Scope Limitation

**IMPORTANT**: Nextcloud does not support OAuth scopes natively. Scopes like "notes:read" are **soft-scopes** enforced by the MCP server via `@require_scopes` decorator, not by the IdP or Nextcloud.

This means:
- Token exchange provides audit and delegation benefits, not scope restriction
- All Nextcloud tokens have equivalent permissions at the Nextcloud level
- Fine-grained access control is enforced by MCP server, not Nextcloud

## Next Actions (Optional Enhancements)

1. [ ] Add integration tests for token exchange mode with actual MCP tools
2. [ ] Document background job patterns for scheduled sync operations
3. [ ] Add metrics for token exchange performance
4. [ ] Consider making token exchange the default in future major version
