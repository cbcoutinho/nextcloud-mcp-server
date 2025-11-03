# CRITICAL: Token Exchange Pattern for ADR-004

## Problem Statement

The current implementation of ADR-004 Progressive Consent does **NOT** correctly implement the token exchange pattern. This is a **critical architectural flaw** that must be corrected.

## Current (Incorrect) Implementation

### What Happens Now:
1. Client gets Flow 1 token (`aud: "mcp-server"`)
2. Client calls MCP tool
3. Server validates Flow 1 token
4. **WRONG**: Server uses stored refresh token to get Nextcloud token
5. **WRONG**: Same refresh token used for all sessions and background jobs

### Problems:
- ❌ No separation between session tokens and background tokens
- ❌ Refresh tokens are reused across different contexts
- ❌ Session tokens could have different scope requirements than background tokens
- ❌ No on-demand delegation during tool calls
- ❌ Violates principle of least privilege

## Correct Implementation Required

### Token Exchange Pattern

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
    requested_scopes: list[str],
    requested_audience: str = "nextcloud"
) -> tuple[str, int]:
    """
    Exchange Flow 1 MCP token for delegated Nextcloud token.

    This implements RFC 8693 Token Exchange for on-behalf-of delegation.

    Args:
        flow1_token: The MCP session token (aud: "mcp-server")
        requested_scopes: Scopes needed for this operation
        requested_audience: Target audience (usually "nextcloud")

    Returns:
        Tuple of (delegated_token, expires_in)
    """
    # 1. Validate Flow 1 token
    # 2. Check user has provisioned Nextcloud access (Flow 2)
    # 3. Request token exchange from IdP
    # 4. Return ephemeral delegated token
```

### 2. Context-Aware Token Broker

Update Token Broker to distinguish contexts:

```python
class TokenBrokerService:
    async def get_session_token(
        self,
        flow1_token: str,
        required_scopes: list[str]
    ) -> str:
        """Get ephemeral token for MCP session (on-demand)."""
        # Exchange Flow 1 token for delegated token
        # DO NOT use stored refresh token
        # Return short-lived token

    async def get_background_token(
        self,
        user_id: str,
        required_scopes: list[str]
    ) -> str:
        """Get token for background job (uses refresh token)."""
        # Use stored refresh token from Flow 2
        # Different scope requirements
        # Longer-lived token
```

### 3. Update MCP Tool Pattern

Tools should request token exchange:

```python
@mcp.tool()
@require_scopes("notes:read")
@require_provisioning
async def nc_notes_search_notes(query: str, ctx: Context) -> SearchNotesResponse:
    """Search notes by title or content."""

    # Extract Flow 1 token from context
    flow1_token = ctx.authorization.token

    # Get Token Broker
    broker = get_token_broker()

    # CRITICAL: Exchange for delegated token
    nextcloud_token = await broker.get_session_token(
        flow1_token=flow1_token,
        required_scopes=["notes:read"]  # Minimal scopes for this operation
    )

    # Create Nextcloud client with delegated token
    client = await create_nextcloud_client(
        host=NEXTCLOUD_HOST,
        token=nextcloud_token  # Ephemeral delegated token
    )

    # Execute operation
    results = await client.notes_search_notes(query=query)

    # Token automatically expires - NOT stored
    return SearchNotesResponse(results=results)
```

### 4. Background Job Pattern

```python
# Background worker
async def sync_notes_job(user_id: str):
    """Background job to sync notes."""

    broker = get_token_broker()

    # CRITICAL: Use background token pattern
    background_token = await broker.get_background_token(
        user_id=user_id,
        required_scopes=["notes:sync", "files:sync"]  # Background-specific scopes
    )

    # Create client with background token
    client = await create_nextcloud_client(
        host=NEXTCLOUD_HOST,
        token=background_token
    )

    # Perform background sync
    await client.notes.sync_all()
```

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

**Current Status**: ❌ CRITICAL ISSUE - Token exchange not implemented
**Target Status**: ✅ Proper token exchange with session/background separation
**Priority**: **P0 - Blocker for production use**

## Next Actions

1. [ ] Implement `token_exchange.py` module with RFC 8693 support
2. [ ] Update `TokenBrokerService` with session vs background methods
3. [ ] Refactor MCP tools to use token exchange pattern
4. [ ] Add integration tests for token exchange
5. [ ] Document background job patterns
6. [ ] Update ADR-004 with implementation details
