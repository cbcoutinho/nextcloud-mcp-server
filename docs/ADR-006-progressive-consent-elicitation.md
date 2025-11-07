# ADR-006: Progressive Consent via URL Elicitation (SEP-1036)

**Status**: Partially Implemented (Interim Workaround)
**Date**: 2025-01-05 (Updated: 2025-01-07)
**Related**: [SEP-1036](https://github.com/modelcontextprotocol/specification/pull/887), ADR-004
**Depends On**: ADR-005 (token validation)

## Context

### What is Progressive Consent?

**Progressive consent is a mechanism, not a feature**. It describes HOW users grant the MCP server access to Nextcloud resources through OAuth elicitation. The server can operate in two modes:

1. **Pass-through mode (ENABLE_OFFLINE_ACCESS=false)**:
   - No refresh tokens requested or stored
   - Server passes through client's access token to Nextcloud
   - No provisioning tools available
   - Suitable for stateless, client-driven operations

2. **Offline access mode (ENABLE_OFFLINE_ACCESS=true)**:
   - Server requests `offline_access` scope and stores refresh tokens
   - Enables background operations and server-initiated API calls
   - Provisioning tools available (`provision_nextcloud_access`, `check_logged_in`)
   - Requires explicit user consent via OAuth Flow 2

**Single-user mode (BasicAuth)** doesn't use progressive consent at all - credentials are directly available.

### Current User Experience Issues

The current offline access provisioning flow (ADR-004) requires users to manually visit OAuth URLs returned by MCP tools. This creates a poor user experience:

1. User calls `provision_nextcloud_access` tool
2. Tool returns a URL as text in the response
3. User must manually copy URL and open in browser
4. No indication when provisioning is complete
5. User must retry the original operation manually

### SEP-1036: URL Mode Elicitation

The MCP specification now supports **URL mode elicitation** ([SEP-1036](https://github.com/modelcontextprotocol/specification/pull/887)), which enables servers to:

- Request out-of-band user interactions via secure URLs
- Handle sensitive operations like OAuth flows without exposing credentials to the client
- Provide progress tracking for async operations
- Return errors that automatically trigger elicitation flows

**Key benefits for progressive consent**:
- **Automatic URL Opening**: Client opens URL in browser automatically (with user consent)
- **Progress Tracking**: Server can notify client when provisioning is complete
- **Error-Triggered Flows**: Server can return `ElicitationRequired` error to trigger provisioning
- **Better UX**: User doesn't manually copy/paste URLs

### Current Implementation Limitations

The current progressive consent flow in `nextcloud_mcp_server/server/oauth_tools.py`:

```python
@mcp.tool(name="provision_nextcloud_access")
async def tool_provision_access(ctx: Context) -> ProvisioningResult:
    """Returns OAuth URL as text - user must manually open it."""
    return ProvisioningResult(
        success=True,
        authorization_url=auth_url,  # User must copy this
        message="Please visit the authorization URL..."
    )
```

**Problems**:
1. Manual URL handling (copy/paste)
2. No progress tracking
3. No automatic retry after provisioning
4. Tool call required just to get URL
5. No client integration (URL just displayed as text)

## Decision

We will **migrate progressive consent from manual tools to URL mode elicitation**, leveraging SEP-1036 for better user experience and OAuth security.

### New Architecture: Elicitation-Driven Consent

Instead of explicit tools, use **automatic elicitation** triggered by authorization errors:

```
User → Calls Nextcloud Tool → Server Checks Provisioning
                                     ↓ Not Provisioned
                                Error: ElicitationRequired
                                     ↓
                          Client Shows Consent UI
                                     ↓ User Accepts
                          Client Opens OAuth URL
                                     ↓
                          User Completes OAuth
                                     ↓
                          Server Sends Progress Update
                                     ↓
                      Original Tool Call Auto-Retries
```

### Mode 1: Elicitation-Required Error (Primary)

When a tool requires provisioning, return an **ElicitationRequired error** (-32000):

```python
# In any Nextcloud tool decorated with @require_provisioning
@mcp.tool()
@require_provisioning  # New decorator
async def nc_notes_list_notes(ctx: Context):
    """List notes - auto-triggers provisioning if needed."""
    # If not provisioned, decorator returns ElicitationRequired error
    # If provisioned, continues normally
    client = await get_client(ctx)
    return await client.notes.list_notes()
```

**Error response structure**:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32000,
    "message": "Nextcloud access provisioning required",
    "data": {
      "elicitations": [
        {
          "mode": "url",
          "elicitationId": "550e8400-e29b-41d4-a716-446655440000",
          "url": "https://mcp.example.com/oauth/provision?id=550e8400...",
          "message": "Grant the MCP server access to your Nextcloud account to continue."
        }
      ]
    }
  }
}
```

**Client behavior**:
1. Receives error with elicitation
2. Shows consent UI: "App wants to access Nextcloud. Open authorization page?"
3. On user acceptance, opens URL in browser
4. Optionally tracks progress via `elicitation/track`
5. Auto-retries original tool call when complete

### Mode 2: Explicit Elicitation Request (Fallback)

For clients that don't support error-triggered elicitation, provide explicit tool:

```python
@mcp.tool(name="request_nextcloud_access")
async def request_access(ctx: Context) -> ElicitationResponse:
    """Explicitly request provisioning via elicitation."""
    # Send elicitation/create request
    return await create_elicitation(
        mode="url",
        url=generate_oauth_url(),
        message="Grant access to Nextcloud",
        elicitation_id=generate_id()
    )
```

**Note**: This is a fallback for compatibility. Primary flow uses error-triggered elicitation.

## Implementation

### 1. New Decorator: `@require_provisioning`

Replace explicit provisioning checks with a decorator that returns `ElicitationRequired`:

```python
# nextcloud_mcp_server/auth/provisioning_decorator.py

def require_provisioning(func):
    """
    Decorator that ensures user has provisioned Nextcloud access.

    If not provisioned, returns ElicitationRequired error with OAuth URL.
    Otherwise, proceeds with normal tool execution.
    """
    @functools.wraps(func)
    async def wrapper(ctx: Context, *args, **kwargs):
        # Extract user ID from token
        user_id = get_user_id_from_context(ctx)

        # Check if provisioned
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        if not await storage.has_refresh_token(user_id):
            # Not provisioned - return ElicitationRequired error
            elicitation_id = str(uuid.uuid4())
            oauth_url = await generate_oauth_url_for_provisioning(
                user_id=user_id,
                elicitation_id=elicitation_id,
                ctx=ctx
            )

            # Store elicitation for tracking
            await storage.store_elicitation(
                elicitation_id=elicitation_id,
                user_id=user_id,
                status="pending",
                created_at=datetime.now(timezone.utc)
            )

            raise McpError(
                code=ErrorCode.ELICITATION_REQUIRED,  # -32000
                message="Nextcloud access provisioning required",
                data={
                    "elicitations": [
                        {
                            "mode": "url",
                            "elicitationId": elicitation_id,
                            "url": oauth_url,
                            "message": (
                                "Grant the MCP server access to your Nextcloud "
                                "account to continue. This is a one-time setup."
                            )
                        }
                    ]
                }
            )

        # Already provisioned - proceed normally
        return await func(ctx, *args, **kwargs)

    return wrapper
```

### 2. Elicitation Tracking Endpoint

Implement `elicitation/track` to provide progress updates:

```python
# nextcloud_mcp_server/server/elicitation.py

@mcp.request_handler("elicitation/track")
async def track_elicitation(
    elicitation_id: str,
    _meta: dict = None
) -> dict:
    """
    Track progress of an elicitation request.

    Returns when elicitation is complete or times out.
    """
    progress_token = _meta.get("progressToken") if _meta else None

    storage = RefreshTokenStorage.from_env()
    await storage.initialize()

    # Poll for completion (with timeout)
    timeout = 300  # 5 minutes
    start_time = datetime.now(timezone.utc)

    while (datetime.now(timezone.utc) - start_time).seconds < timeout:
        elicitation = await storage.get_elicitation(elicitation_id)

        if not elicitation:
            raise McpError(
                code=-32602,  # Invalid params
                message=f"Unknown elicitation ID: {elicitation_id}"
            )

        # Send progress notification if token provided
        if progress_token and elicitation["status"] == "pending":
            await send_progress_notification(
                progress_token=progress_token,
                progress=50,
                message="Waiting for OAuth authorization..."
            )

        # Check if complete
        if elicitation["status"] == "complete":
            return {"status": "complete"}

        # Check if failed
        if elicitation["status"] == "failed":
            return {
                "status": "failed",
                "error": elicitation.get("error_message")
            }

        # Wait before polling again
        await asyncio.sleep(2)

    # Timeout
    raise McpError(
        code=-32000,
        message="Elicitation timed out - user did not complete authorization"
    )
```

### 3. OAuth Callback Updates

Update the OAuth callback to mark elicitations as complete:

```python
# nextcloud_mcp_server/auth/oauth_routes.py

async def oauth_callback(request: Request) -> Response:
    """Handle OAuth callback and mark elicitation complete."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    # Validate and exchange code for tokens
    tokens = await exchange_authorization_code(code)

    # Store refresh token
    await storage.store_refresh_token(
        user_id=user_id,
        refresh_token=tokens["refresh_token"]
    )

    # Mark elicitation as complete
    elicitation_id = request.query_params.get("elicitation_id")
    if elicitation_id:
        await storage.update_elicitation(
            elicitation_id=elicitation_id,
            status="complete",
            completed_at=datetime.now(timezone.utc)
        )

    return Response(
        content="<h1>Authorization Complete!</h1>"
        "<p>You can close this window and return to the application.</p>",
        media_type="text/html"
    )
```

### 4. Update All Nextcloud Tools

Add `@require_provisioning` decorator to all Nextcloud tools:

```python
# nextcloud_mcp_server/server/notes.py

@mcp.tool()
@require_scopes("notes:read")
@require_provisioning  # NEW: Auto-triggers provisioning
async def nc_notes_list_notes(
    ctx: Context,
    category: Optional[str] = None
) -> NotesListResponse:
    """List all notes - automatically handles provisioning."""
    client = await get_client(ctx)
    # Tool logic proceeds only if provisioned
    notes = await client.notes.list_notes(category=category)
    return NotesListResponse(results=notes)
```

### 5. Capability Declaration

Declare URL elicitation support during initialization:

```python
# nextcloud_mcp_server/app.py

capabilities = {
    "elicitation": {
        "url": {}  # Declare URL mode support
        # Note: We don't support "form" mode (in-band data collection)
    },
    # ... other capabilities
}
```

### 6. Environment Variables

**Primary control**:
```bash
# ENABLE_OFFLINE_ACCESS: Controls whether server requests refresh tokens and enables provisioning tools
# Default: false (pass-through mode)
# Set to true to enable offline access mode with Flow 2 provisioning
ENABLE_OFFLINE_ACCESS=true
```

**Future variables** (when URL elicitation is implemented):
```bash
# ELICITATION_CALLBACK_URL: Base URL for OAuth callbacks with elicitation tracking
# Default: NEXTCLOUD_MCP_SERVER_URL + /oauth/callback
ELICITATION_CALLBACK_URL=http://localhost:8000/oauth/callback

# ELICITATION_TIMEOUT_SECONDS: How long to wait for user to complete OAuth
# Default: 300 (5 minutes)
ELICITATION_TIMEOUT_SECONDS=300
```

**Removed variables**:
```bash
# ENABLE_PROGRESSIVE_CONSENT - Removed. Progressive consent is a mechanism, not a feature toggle.
#                               Use ENABLE_OFFLINE_ACCESS to control whether provisioning tools are available.
# MCP_SERVER_CLIENT_ID - merged into OIDC_CLIENT_ID
```

## User Experience Comparison

### Before (ADR-004 Manual Tools)

```
User: "List my notes"
Assistant: *calls nc_notes_list_notes*
Server: Error - not provisioned
Assistant: "You need to provision access first. Let me do that."
Assistant: *calls provision_nextcloud_access*
Server: {authorization_url: "https://..."}
Assistant: "Please visit this URL: https://..."
User: *copies URL, opens browser, completes OAuth*
User: "OK, I'm done"
Assistant: *calls nc_notes_list_notes again*
Server: Success! [notes...]
```

**Issues**: 4 interactions, manual URL handling, no automation

### After (ADR-006 Elicitation)

```
User: "List my notes"
Assistant: *calls nc_notes_list_notes*
Server: ElicitationRequired error
Client: Shows dialog: "Grant access to Nextcloud? [Yes] [No]"
User: *clicks Yes*
Client: Opens OAuth URL in browser automatically
User: *completes OAuth*
Server: Sends progress notification "Complete!"
Client: Auto-retries nc_notes_list_notes
Server: Success! [notes...]
Assistant: "Here are your notes: ..."
```

**Benefits**: 1 interaction, automatic URL opening, seamless retry

## Migration Path

### Phase 1: Add Elicitation Support (v0.26.0)

- Implement `@require_provisioning` decorator
- Add `elicitation/track` endpoint
- Keep existing tools (`provision_nextcloud_access`) for compatibility
- Update OAuth callback to track elicitations
- Add capability declaration

**Breaking changes**: None (additive)

### Phase 2: Update Documentation (v0.27.0)

- Document elicitation-based flow as primary
- Mark manual tools as deprecated
- Update examples and guides

**Breaking changes**: None (documentation only)

### Phase 3: Remove Manual Tools (v0.28.0)

- Remove `provision_nextcloud_access` tool
- Remove `check_provisioning_status` tool (status in error message)
- Remove `revoke_nextcloud_access` (or keep for explicit revocation?)

**Breaking changes**: Yes (removed tools)

### Phase 4: Optimize (v0.29.0+)

- Add elicitation result caching
- Implement retry strategies
- Add metrics and monitoring

## Testing

### Test Cases

1. **First-Time User Flow**
   ```python
   @pytest.mark.oauth
   async def test_elicitation_first_time_user(nc_mcp_oauth_client):
       """Test that first tool call triggers elicitation."""
       # User has no provisioning
       with pytest.raises(McpError) as exc:
           await nc_mcp_oauth_client.call_tool("nc_notes_list_notes")

       # Should get ElicitationRequired error
       assert exc.value.code == -32000
       assert "elicitations" in exc.value.data
       assert exc.value.data["elicitations"][0]["mode"] == "url"

       # Verify URL is valid OAuth URL
       url = exc.value.data["elicitations"][0]["url"]
       assert "oauth" in url
       assert "elicitationId" in url
   ```

2. **Progress Tracking**
   ```python
   @pytest.mark.oauth
   async def test_elicitation_progress_tracking(nc_mcp_oauth_client):
       """Test progress tracking during OAuth flow."""
       # Trigger elicitation
       elicitation_id = trigger_elicitation()

       # Start tracking
       track_task = asyncio.create_task(
           nc_mcp_oauth_client.track_elicitation(
               elicitation_id=elicitation_id,
               progress_token="test-token"
           )
       )

       # Simulate OAuth completion
       await asyncio.sleep(1)
       await complete_oauth_flow(elicitation_id)

       # Track should complete
       result = await track_task
       assert result["status"] == "complete"
   ```

3. **Auto-Retry After Provisioning**
   ```python
   @pytest.mark.oauth
   async def test_auto_retry_after_provisioning(nc_mcp_oauth_client):
       """Test that client auto-retries after elicitation."""
       # Mock client that auto-retries on ElicitationRequired
       client = AutoRetryMcpClient(nc_mcp_oauth_client)

       # First call triggers elicitation, client handles it, retries
       result = await client.call_tool_with_elicitation("nc_notes_list_notes")

       # Should succeed after provisioning
       assert result.success
       assert "notes" in result.data
   ```

4. **Timeout Handling**
   ```python
   @pytest.mark.oauth
   async def test_elicitation_timeout(nc_mcp_oauth_client):
       """Test timeout if user doesn't complete OAuth."""
       elicitation_id = trigger_elicitation()

       # Track with short timeout
       with pytest.raises(McpError, match="timed out"):
           await nc_mcp_oauth_client.track_elicitation(
               elicitation_id=elicitation_id,
               timeout=5  # 5 seconds
           )
   ```

## Security Considerations

### Out-of-Band OAuth Flow

**Benefit**: OAuth credentials never pass through MCP client
- User enters credentials directly on IdP page
- MCP server receives only authorization code
- Client never sees passwords or refresh tokens

**Threat mitigation**:
- **Credential theft**: Client can't intercept credentials (out-of-band)
- **Token exposure**: Client never receives Nextcloud refresh tokens
- **CSRF**: State parameter validates OAuth callback
- **URL tampering**: Elicitation ID ties OAuth flow to user session

### Elicitation ID as Security Token

The `elicitationId` serves as a capability token:
- Cryptographically random (UUID v4)
- Single-use (invalidated after completion)
- Time-limited (expires after timeout)
- User-scoped (tied to user session)

**Validation**:
```python
async def validate_elicitation_id(elicitation_id: str, user_id: str) -> bool:
    """Validate that elicitation belongs to user and is still valid."""
    elicitation = await storage.get_elicitation(elicitation_id)

    if not elicitation:
        return False

    # Check ownership
    if elicitation["user_id"] != user_id:
        logger.warning(f"Elicitation ID mismatch: {elicitation_id}")
        return False

    # Check expiry
    if elicitation["expires_at"] < datetime.now(timezone.utc):
        return False

    # Check not already used
    if elicitation["status"] != "pending":
        return False

    return True
```

### Progress Tracking Security

**Risk**: Progress token reuse across users

**Mitigation**:
- Progress tokens tied to elicitation ID
- Elicitation ID tied to user session
- Server validates ownership before sending updates

## Consequences

### Positive

1. **Better UX**: Automatic URL opening, no manual copy/paste
2. **Seamless Flow**: Auto-retry after provisioning
3. **Progress Feedback**: User knows when OAuth is complete
4. **Spec Compliance**: Implements SEP-1036 correctly
5. **Secure by Design**: Out-of-band OAuth prevents credential exposure
6. **Simpler API**: No explicit provisioning tools needed

### Negative

1. **Client Dependency**: Requires client support for URL elicitation
2. **Complexity**: More moving parts (elicitation tracking, callbacks)
3. **Polling**: Progress tracking uses polling (not ideal)
4. **Breaking Change**: Removes manual provisioning tools (in v0.28.0)

### Neutral

1. **Storage Requirements**: Need to store elicitation state
2. **Timeout Management**: Must handle long-running OAuth flows
3. **Fallback Support**: Still need compatibility for older clients

## Alternatives Considered

### 1. Keep Manual Tools Only (Rejected)

**Pros**: Simple, no client changes needed
**Cons**: Poor UX, doesn't leverage SEP-1036

**Rejection reason**: SEP-1036 provides better UX and security

### 2. Form Mode Elicitation (Rejected)

**Pros**: No browser redirect needed
**Cons**: Would expose OAuth credentials to client (security violation)

**Rejection reason**: Form mode only for non-sensitive data per SEP-1036

### 3. Hybrid: Both Tools and Elicitation (Considered)

**Pros**: Maximum compatibility, gradual migration
**Cons**: API duplication, maintenance burden, confusing for users

**Decision**: Support during migration (v0.26-0.27), remove in v0.28

### 4. WebSocket for Progress (Rejected)

**Pros**: Real-time updates instead of polling
**Cons**: MCP spec uses polling pattern, adds complexity

**Rejection reason**: Follow spec pattern (polling via elicitation/track)

## Interim Implementation: Inline Form Elicitation (Pre-SEP-1036)

**Note**: SEP-1036 (URL mode elicitation) is not yet available in the stable MCP Python SDK. As a temporary workaround, we've implemented a simplified version using the current **inline form elicitation** API.

### What Changed

Instead of waiting for URL mode elicitation, we implemented a `check_logged_in` tool that:

1. Checks if the user has completed Flow 2 (resource provisioning)
2. If logged in, returns `"yes"`
3. If not logged in, uses **inline form elicitation** to prompt the user

### Implementation Details

**New Tool**: `check_logged_in`

```python
# nextcloud_mcp_server/server/oauth_tools.py

class LoginConfirmation(BaseModel):
    """Schema for login confirmation elicitation."""
    acknowledged: bool = Field(
        default=False,
        description="Check this box after completing login at the provided URL",
    )

@mcp.tool(name="check_logged_in")
@require_scopes("openid")
async def tool_check_logged_in(ctx: Context, user_id: Optional[str] = None) -> str:
    """Check if user is logged in and elicit login if needed."""
    # Check if already logged in
    status = await get_provisioning_status(ctx, user_id)
    if status.is_provisioned:
        return "yes"

    # Generate OAuth URL for Flow 2
    auth_url = generate_oauth_url_for_flow2(...)

    # Use inline form elicitation (current MCP API)
    result = await ctx.elicit(
        message=f"Please log in to Nextcloud at the following URL:\n\n{auth_url}\n\nAfter completing the login, check the box below and click OK.",
        schema=LoginConfirmation,
    )

    if result.action == "accept":
        # Verify login succeeded
        status = await get_provisioning_status(ctx, user_id)
        return "yes" if status.is_provisioned else "Login not detected"
    elif result.action == "decline":
        return "Login declined by user."
    else:
        return "Login cancelled by user."
```

**OAuth Routes** (added to `app.py`):

```python
# Flow 2 routes for resource provisioning
routes.append(
    Route("/oauth/authorize-nextcloud", oauth_authorize_nextcloud, methods=["GET"])
)
routes.append(
    Route("/oauth/callback-nextcloud", oauth_callback_nextcloud, methods=["GET"])
)
```

### User Experience

```
User: *calls check_logged_in tool*

MCP Client: Displays form elicitation
┌─────────────────────────────────────────────────────────┐
│ Please log in to Nextcloud at the following URL:      │
│                                                         │
│ http://localhost:8000/oauth/authorize-nextcloud?...    │
│                                                         │
│ After completing the login, check the box below and    │
│ click OK.                                               │
│                                                         │
│ ☐ Check this box after completing login                │
│                                                         │
│ [Accept] [Decline] [Cancel]                            │
└─────────────────────────────────────────────────────────┘

User: *copies URL, opens in browser, completes OAuth*
User: *checks box and clicks Accept*

MCP Server: Verifies login and returns "yes"
```

### Limitations of Interim Approach

1. **Manual URL Handling**: User must manually copy and paste the URL (not clickable)
2. **No Automatic Browser Opening**: Client doesn't automatically open the URL
3. **No Progress Tracking**: Can't track OAuth completion status in real-time
4. **URL in Message Text**: Login URL embedded in plain text message (not as structured field)
5. **Client-Side Confirmation**: Relies on user clicking "OK" after OAuth (honor system)

### Why Not Use URL Mode Now?

The current stable MCP Python SDK (`main` branch) only supports **inline form elicitation**:

```python
# Current API (no 'mode' parameter)
class ElicitRequestParams(RequestParams):
    message: str
    requestedSchema: ElicitRequestedSchema
    # No 'mode', 'url', or 'elicitationId' fields
```

URL mode elicitation (`mode: "url"`) is only available in the SEP-1036 branch, which has not been merged to `main` yet.

### Migration to URL Mode (When SEP-1036 Lands)

Once SEP-1036 is merged and available in the stable SDK, we will migrate to URL mode elicitation:

**Before (Current Workaround)**:
```python
result = await ctx.elicit(
    message=f"Please log in at: {auth_url}\n\nClick OK after login.",
    schema=LoginConfirmation,
)
```

**After (URL Mode)**:
```python
result = await ctx.session.elicit_url(
    message="Please log in to Nextcloud to authorize this MCP server.",
    url=auth_url,
    elicitation_id=elicitation_id,
)
```

**Benefits of migration**:
- Automatic URL opening (with user consent)
- Clickable URLs in client UI
- Progress tracking via `elicitation/track`
- Better security (URL not in message text)
- Auto-retry support

### Testing

Integration tests validate the current inline form elicitation:

```python
# tests/server/oauth/test_login_elicitation.py

async def test_check_logged_in_already_authenticated(nc_mcp_oauth_client):
    """Test immediate 'yes' for authenticated users."""
    result = await nc_mcp_oauth_client.call_tool("check_logged_in", arguments={})
    assert "yes" in result.content[0].text.lower()

async def test_check_logged_in_url_format(nc_mcp_oauth_client):
    """Test that login URL (when needed) contains correct OAuth parameters."""
    result = await nc_mcp_oauth_client.call_tool("check_logged_in", arguments={})
    response_text = result.content[0].text

    # If URL present, validate OAuth parameters
    if "http" in response_text:
        assert "response_type=code" in response_text
        assert "client_id=" in response_text
        assert "redirect_uri=" in response_text
        assert "openid" in response_text
```

### Future Work

- **Monitor SEP-1036**: Watch for merge to MCP Python SDK `main` branch
- **Implement URL Mode**: Once available, migrate `check_logged_in` to use `ctx.session.elicit_url()`
- **Add Progress Tracking**: Implement `elicitation/track` endpoint for OAuth completion status
- **Implement Error-Triggered Elicitation**: Use `@require_provisioning` decorator to return `ElicitationRequired` errors
- **Remove Manual Workaround**: Deprecate inline form approach once URL mode is stable

## References

- [SEP-1036: URL Mode Elicitation](https://github.com/modelcontextprotocol/specification/pull/887)
- [MCP Elicitation Specification](https://modelcontextprotocol.io/specification/draft/client/elicitation)
- [ADR-004: Federated Authentication Architecture](./ADR-004-mcp-application-oauth.md)
- [ADR-005: Token Audience Validation](./ADR-005-token-audience-validation.md)
- [RFC 8252: OAuth 2.0 for Native Apps](https://datatracker.ietf.org/doc/html/rfc8252)

## Implementation Checklist

### Interim Implementation (Inline Form Elicitation)

- [x] Create `check_logged_in` tool with inline form elicitation
- [x] Register Flow 2 OAuth routes (`/oauth/authorize-nextcloud`, `/oauth/callback-nextcloud`)
- [x] Write integration tests for login elicitation flow
- [x] Update ADR-006 with interim implementation documentation
- [x] Add `LoginConfirmation` schema for elicitation
- [ ] Run tests to validate implementation

### Future Work (URL Mode Elicitation - Post SEP-1036)

- [ ] Implement `@require_provisioning` decorator with ElicitationRequired error
- [ ] Add `elicitation/track` request handler
- [ ] Update OAuth callback to mark elicitations complete
- [ ] Add elicitation storage (ID, user, status, timestamps)
- [ ] Update all Nextcloud tools with `@require_provisioning`
- [ ] Add URL elicitation capability declaration
- [ ] Write tests for progress tracking
- [ ] Update documentation with URL mode examples
- [ ] Add migration guide for manual tools → elicitation
- [ ] Migrate `check_logged_in` from inline form to URL mode
- [ ] Keep manual tools with deprecation warnings (v0.26-0.27)
- [ ] Remove manual tools (v0.28.0)
- [ ] Update CHANGELOG.md with migration timeline
