# ADR-022: Deployment Mode Consolidation via Login Flow v2

**Status:** Proposed
**Date:** 2026-02-01
**Deciders:** Development Team
**Related:** ADR-020 (Deployment Modes), ADR-021 (Configuration Consolidation), ADR-004 (Progressive Consent), Issue #521

## Context

The Nextcloud MCP Server currently supports five distinct deployment modes (ADR-020):

1. **Single-User BasicAuth** - App password in environment variables
2. **Multi-User BasicAuth** - HTTP header credential pass-through
3. **OAuth Single-Audience** - Multi-audience token validation
4. **OAuth Token Exchange** - RFC 8693 delegation
5. **Smithery Stateless** - Session URL parameters

This complexity creates several problems:

### Maintenance Burden

- Configuration validation requires ~460 lines of code with mode-specific logic
- Each mode has different conditional requirements and forbidden variables
- Documentation must cover 5 different deployment paths
- Testing requires separate containers for each mode (`mcp`, `mcp-oauth`, `mcp-keycloak`)

### Security Anti-Patterns

- **Multi-User BasicAuth** passes user credentials through the MCP server (credential exposure risk)
- **OAuth modes** require upstream patches to Nextcloud for Bearer token validation on non-OCS endpoints
- Token passthrough creates audit trail issues (actions attributed to MCP server, not user)

### Adoption Barriers

- OAuth modes require patched `user_oidc` app or complex IdP configuration
- Configuration matrix has ~500+ possible combinations
- Users struggle to select the appropriate mode for their use case

### Critical Insight: Nextcloud App Passwords

Nextcloud's app password system provides a simple, native mechanism for delegated API access:

- **Universal compatibility**: Works on ANY Nextcloud instance (NC 16+)
- **No upstream patches required**: Uses standard Nextcloud APIs
- **User-visible**: Appears in Settings > Security > Devices & Sessions
- **User-revocable**: Users can revoke access at any time
- **Proven pattern**: Used by all official Nextcloud clients (Desktop, Mobile)

**However**, app passwords have **no native scope support** - they grant full API access equivalent to the user's permissions. This is a critical security consideration that requires application-level mitigation.

### Nextcloud Platform Limitation

> **Important**: Nextcloud does not support scoped app passwords, and OAuth bearer token support varies by endpoint type. This is a platform limitation, not an MCP server design choice.
>
> **OAuth Bearer Token Support by Endpoint:**
> | Endpoint Type | OAuth Bearer Supported | Scoped Access |
> |---------------|------------------------|---------------|
> | OCS API | ✅ Yes | ❌ No |
> | WebDAV | ✅ Yes | ❌ No |
> | CalDAV/CardDAV | ❌ No | ❌ No |
> | Notes API | ❌ No | ❌ No |
> | Other App APIs | ❌ No | ❌ No |
>
> **Implications:**
> - App passwords grant full API access to any Nextcloud API the user can access
> - Even where OAuth tokens are accepted, scopes are not enforced by Nextcloud
> - There are no upstream plans to add scoped OAuth support to App APIs
>
> **Our approach:** The MCP server implements application-level scope enforcement as a defense-in-depth measure. This provides audit logging, user transparency, and protection against accidental misuse, but administrators must understand that scope enforcement occurs at the MCP server layer, not the Nextcloud layer.
>
> If Nextcloud adds scoped OAuth support for App APIs in the future, this architecture will be revisited to leverage native scope enforcement.

## Decision

Consolidate deployment modes into **two simplified modes**:

### Mode 1: Single-User Mode

**Use Case:** Personal Nextcloud, local development, single-tenant deployments

**Configuration:**
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
NEXTCLOUD_APP_PASSWORD=xxxxx-xxxxx-xxxxx-xxxxx-xxxxx
NEXTCLOUD_USERNAME=admin  # Optional, can be inferred from app password
```

**Characteristics:**
- App password configured in environment variables
- No persistent state required (stateless)
- No Login Flow v2 (credentials pre-configured)
- All MCP tools available (no scope enforcement - trusted environment)
- Suitable for trusted environments only

### Mode 2: Multi-User Mode

**Use Case:** Multi-user deployments, enterprise, shared instances

**Architecture:**
```
┌─────────────────┐    OAuth/OIDC    ┌──────────────────┐   Login Flow v2   ┌─────────────────┐
│   MCP Client    │ ───────────────> │   MCP Server     │ ────────────────> │   Nextcloud     │
│   (Claude)      │   (mcp:* scopes) │   (OAuth Client) │   (app password)  │   (NC 16+)      │
└─────────────────┘                  └──────────────────┘                   └─────────────────┘
```

**Configuration:**
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
MCP_DEPLOYMENT_MODE=multi_user  # Or auto-detected when NEXTCLOUD_APP_PASSWORD not set

# Required for app password storage
TOKEN_ENCRYPTION_KEY=<fernet-key>
TOKEN_STORAGE_DB=/app/data/tokens.db

# Optional: Semantic search
ENABLE_SEMANTIC_SEARCH=true
QDRANT_URL=http://qdrant:6333
```

**Characteristics:**
- MCP clients authenticate to MCP server via OAuth (Nextcloud as IdP)
- Per-user app password acquisition via Nextcloud Login Flow v2
- Application-level scope enforcement (critical - see Security Considerations)
- Encrypted app password storage in SQLite
- Background sync uses stored app passwords

### Authentication Flow (Multi-User Mode)

```
┌─────────────────┐                  ┌──────────────────┐                  ┌─────────────────┐
│   MCP Client    │                  │   MCP Server     │                  │   Nextcloud     │
│   (Claude)      │                  │   (OAuth Client) │                  │   (NC 16+)      │
└────────┬────────┘                  └────────┬─────────┘                  └────────┬────────┘
         │                                    │                                     │
         │ 1. OAuth PKCE (mcp:* scopes)       │                                     │
         ├───────────────────────────────────>│                                     │
         │                                    │                                     │
         │ 2. MCP Request (no app password)   │                                     │
         ├───────────────────────────────────>│                                     │
         │                                    │                                     │
         │ 3. Elicitation Response            │                                     │
         │<───────────────────────────────────┤                                     │
         │ "Visit: <login-flow-url>"          │                                     │
         │                                    │                                     │
         │ 4. User clicks URL                 │                                     │
         │                                    │                                     │
         │                                    │ 5. POST /login/v2                   │
         │                                    ├────────────────────────────────────>│
         │                                    │                                     │
         │                                    │ 6. {poll_endpoint, login_url}       │
         │                                    │<────────────────────────────────────│
         │                                    │                                     │
         │ 7. User authenticates in browser   │                                     │
         │────────────────────────────────────┼────────────────────────────────────>│
         │                                    │                                     │
         │                                    │ 8. Poll for completion              │
         │                                    ├────────────────────────────────────>│
         │                                    │                                     │
         │                                    │ 9. {loginName, appPassword}         │
         │                                    │<────────────────────────────────────│
         │                                    │                                     │
         │                                    │ 10. Store encrypted + scopes        │
         │                                    │                                     │
         │ 11. Retry MCP request              │                                     │
         ├───────────────────────────────────>│                                     │
         │                                    │                                     │
         │                                    │ 12. Validate scopes, use app pass   │
         │                                    ├────────────────────────────────────>│
         │                                    │ Authorization: Basic <app-password> │
         │                                    │                                     │
         │ 13. Return result                  │                                     │
         │<───────────────────────────────────┤                                     │
```

### What is Nextcloud Login Flow v2?

Login Flow v2 is Nextcloud's native authentication mechanism for desktop and mobile clients. It provides browser-based authentication without requiring the client to handle credentials directly.

**API Flow:**
1. Client `POST /index.php/login/v2` with `User-Agent` header
2. Server returns `{poll: {endpoint, token}, login: <url>}`
3. User visits `login` URL in their browser, authenticates normally
4. Client polls `endpoint` with `token`
5. On success: `{server, loginName, appPassword}`
6. App password is generated with name from User-Agent (visible in Nextcloud Settings)

**Key benefits:**
- **Browser-based auth**: User authenticates using familiar Nextcloud login
- **No credential handling**: Client never sees username/password
- **Works everywhere**: Available on all Nextcloud 16+ instances
- **User visibility**: App passwords appear in Settings > Security > Devices & Sessions
- **User control**: Users can revoke access anytime without admin intervention

## Architecture Details

### Login Flow v2 MCP Tools

Two new MCP tools enable the provisioning flow:

```python
@mcp.tool(
    title="Provision Nextcloud Access",
    annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
)
async def nc_auth_provision_access(
    ctx: Context,
    requested_scopes: list[str] | None = None,
) -> ProvisionAccessResponse:
    """
    Initiate Nextcloud access provisioning via Login Flow v2.

    The user will be prompted to authorize access in their browser.
    Once complete, call nc_auth_check_status to confirm provisioning.

    Args:
        requested_scopes: Scopes to request (e.g., ["notes:read", "notes:write"]).
                         Defaults to all scopes the MCP client requested.

    Returns:
        Authorization URL to visit and polling status endpoint.
    """
    user_id = extract_user_from_mcp_token(ctx)

    # Determine scopes to request
    if requested_scopes is None:
        # Use scopes from MCP token
        requested_scopes = get_access_token_scopes(ctx)

    # Validate requested scopes against supported scopes
    supported = set(discover_all_scopes(mcp))
    invalid = set(requested_scopes) - supported
    if invalid:
        raise ValueError(f"Invalid scopes: {invalid}")

    # Initiate Login Flow v2
    response = await httpx.post(
        f"{settings.nextcloud_host}/index.php/login/v2",
        headers={"User-Agent": f"Nextcloud MCP Server (user:{user_id})"},
    )
    data = response.json()

    # Store poll session with requested scopes
    await storage.store_login_flow_session(
        user_id=user_id,
        poll_token=data["poll"]["token"],
        poll_endpoint=data["poll"]["endpoint"],
        requested_scopes=requested_scopes,
        expires_at=int(time.time()) + 600,  # 10 min TTL
    )

    return ProvisionAccessResponse(
        status="authorization_required",
        authorization_url=data["login"],
        message="Please visit the URL to authorize Nextcloud access.",
        requested_scopes=requested_scopes,
    )


@mcp.tool(
    title="Check Provisioning Status",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def nc_auth_check_status(ctx: Context) -> ProvisionStatusResponse:
    """
    Check if Nextcloud access provisioning is complete.

    Polls the Login Flow v2 endpoint to check authorization status.
    """
    user_id = extract_user_from_mcp_token(ctx)

    # Check for existing app password
    existing = await storage.get_app_password_with_scopes(user_id)
    if existing:
        return ProvisionStatusResponse(
            status="provisioned",
            message="Access already provisioned.",
            scopes=existing["scopes"],
        )

    # Get pending login flow session
    session = await storage.get_login_flow_session(user_id)
    if not session:
        return ProvisionStatusResponse(
            status="not_initiated",
            message="No provisioning in progress. Call nc_auth_provision_access first.",
        )

    # Poll the endpoint
    response = await httpx.post(
        session["poll_endpoint"],
        data={"token": session["poll_token"]},
    )

    if response.status_code == 404:
        return ProvisionStatusResponse(
            status="pending",
            message="Waiting for user authorization.",
        )

    if response.status_code == 200:
        data = response.json()

        # Store app password WITH SCOPES
        await storage.store_app_password(
            user_id=user_id,
            username=data["loginName"],
            app_password=data["appPassword"],
            scopes=session["requested_scopes"],  # Critical: store authorized scopes
        )

        # Clean up session
        await storage.delete_login_flow_session(user_id)

        return ProvisionStatusResponse(
            status="provisioned",
            message="Access successfully provisioned.",
            scopes=session["requested_scopes"],
        )

    return ProvisionStatusResponse(
        status="error",
        message=f"Authorization failed: {response.status_code}",
    )
```

### MCP Elicitation for Login Flow v2

The MCP protocol supports **elicitation** - a mechanism for servers to request that clients prompt users for input or actions. The MCP specification (2025-11-25) defines two elicitation modes:

- **Form mode**: Structured data collection through the MCP client
- **URL mode**: Out-of-band interactions via external URLs (e.g., OAuth flows)

#### Capability Negotiation

Clients declare elicitation support during session initialization:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {
      "elicitation": {
        "form": {},
        "url": {}
      }
    }
  }
}
```

**Important**: URL mode elicitation (`elicitation.url`) is **not widely supported** by MCP clients as of early 2026. The MCP server MUST gracefully handle clients that:
- Declare no elicitation capability
- Declare only `form` mode support
- Declare `url` mode but fail to open URLs

#### Implementation with Graceful Fallback

The server checks client capabilities and falls back to a message-based approach when URL elicitation is unavailable:

```python
from mcp.types import ElicitResult, ElicitRequest

async def handle_nextcloud_access(ctx: Context, user_id: str) -> ElicitResult | ProvisioningRequiredError:
    """Check if user needs to provision access, return elicitation or error with URL."""

    app_password = await storage.get_app_password_with_scopes(user_id)
    if app_password is not None:
        return None  # Already provisioned

    # Initiate Login Flow v2
    response = await httpx.post(
        f"{settings.nextcloud_host}/index.php/login/v2",
        headers={"User-Agent": f"Nextcloud MCP Server (user:{user_id})"},
    )
    data = response.json()
    login_url = data["login"]

    # Store session for polling
    await storage.store_login_flow_session(
        user_id=user_id,
        poll_token=data["poll"]["token"],
        poll_endpoint=data["poll"]["endpoint"],
        requested_scopes=get_access_token_scopes(ctx),
        expires_at=int(time.time()) + 600,
    )

    # Check client capabilities for URL elicitation
    client_capabilities = get_client_capabilities(ctx)
    supports_url_elicitation = (
        client_capabilities.get("elicitation", {}).get("url") is not None
    )

    if supports_url_elicitation:
        # Preferred: Use URL elicitation for seamless UX
        return ElicitResult(
            mode="url",
            elicitationId=str(uuid.uuid4()),
            url=login_url,
            message=(
                "To access Nextcloud resources, please authorize this application. "
                "Click the link to open Nextcloud in your browser and complete authentication."
            ),
        )
    else:
        # Fallback: Return error with URL in message for manual copy/paste
        raise ProvisioningRequiredError(
            f"Nextcloud access not provisioned. Please visit the following URL to authorize:\n\n"
            f"    {login_url}\n\n"
            f"After completing authentication in your browser, retry your request."
        )
```

#### Client Behavior Expectations

| Client Capability | Server Behavior | User Experience |
|-------------------|-----------------|-----------------|
| `elicitation.url` supported | Returns URL elicitation | Client opens URL automatically or presents clickable link |
| `elicitation.form` only | Returns error with URL in message | User copies URL and pastes in browser |
| No elicitation support | Returns error with URL in message | User copies URL and pastes in browser |

**Fallback UX**: Even without URL elicitation support, users can complete the Login Flow by copying the URL from the error message. This ensures the feature works with any MCP client, though with slightly degraded UX.

#### Retry Behavior

After the user completes authentication in their browser:
1. User retries the original MCP request
2. Server polls Login Flow v2 endpoint and detects completion
3. Server stores app password with requested scopes
4. Original request proceeds normally

### Re-Authentication for Scope Updates

Users may need to update their authorized scopes after initial provisioning. The system supports **re-authentication** with scope merging.

#### Re-auth Scenarios

| Scenario | Trigger | User Action Required |
|----------|---------|----------------------|
| **Initial provisioning** | User has no app password | Complete Login Flow v2 |
| **Scope expansion** | Tool requires scope user hasn't authorized | Re-authenticate to add scopes |
| **Scope reduction** | User wants to revoke specific scopes | Revoke and re-provision with fewer scopes |
| **Token rotation** | Admin policy or user preference | Re-authenticate (new app password issued) |

#### Scope Merging Behavior

When a user re-authenticates to add scopes:

1. **Existing scopes are preserved**: New scopes are merged with existing scopes
2. **Old app password is revoked**: Nextcloud revokes the previous app password
3. **New app password issued**: User authenticates via Login Flow v2
4. **Merged scopes stored**: Both old and new scopes associated with new password

```
Initial:    [notes:read]
Request:    [calendar:read, calendar:write]
Result:     [notes:read, calendar:read, calendar:write]
```

**Note**: Scope reduction requires explicit revocation. Users cannot "downgrade" scopes without fully revoking and re-provisioning.

#### Re-auth Tool Implementation

```python
@mcp.tool(
    title="Update Nextcloud Access Scopes",
    annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
)
async def nc_auth_update_scopes(
    ctx: Context,
    additional_scopes: list[str],
) -> ProvisionAccessResponse:
    """
    Request additional Nextcloud access scopes.

    If the user already has provisioned access, this initiates a new Login Flow v2
    to authorize additional scopes. The new scopes will be MERGED with existing scopes.

    Args:
        additional_scopes: New scopes to add (e.g., ["calendar:read", "calendar:write"]).

    Returns:
        Authorization URL to visit for scope upgrade.
    """
    user_id = extract_user_from_mcp_token(ctx)

    # Get existing scopes
    existing = await storage.get_app_password_with_scopes(user_id)
    existing_scopes = set(existing["scopes"]) if existing else set()

    # Validate new scopes
    supported = set(discover_all_scopes(mcp))
    invalid = set(additional_scopes) - supported
    if invalid:
        raise ValueError(f"Invalid scopes: {invalid}")

    # Merge scopes
    merged_scopes = list(existing_scopes | set(additional_scopes))

    # Check if any new scopes actually needed
    if set(additional_scopes) <= existing_scopes:
        return ProvisionAccessResponse(
            status="already_authorized",
            message="All requested scopes are already authorized.",
            scopes=list(existing_scopes),
        )

    # Revoke old app password (will be replaced)
    if existing:
        await _revoke_nextcloud_app_password(existing["username"], existing["app_password"])
        await storage.delete_app_password(user_id)

    # Initiate new Login Flow v2 with merged scopes
    response = await httpx.post(
        f"{settings.nextcloud_host}/index.php/login/v2",
        headers={"User-Agent": f"Nextcloud MCP Server (user:{user_id}, scope-update)"},
    )
    data = response.json()

    await storage.store_login_flow_session(
        user_id=user_id,
        poll_token=data["poll"]["token"],
        poll_endpoint=data["poll"]["endpoint"],
        requested_scopes=merged_scopes,  # Merged scopes
        expires_at=int(time.time()) + 600,
    )

    return ProvisionAccessResponse(
        status="authorization_required",
        authorization_url=data["login"],
        message=f"Please re-authorize to add scopes: {additional_scopes}",
        requested_scopes=merged_scopes,
        previous_scopes=list(existing_scopes),
    )
```

#### Automatic Re-auth Prompting

When a tool requires a scope the user hasn't authorized, the `@require_scopes` decorator returns an error with re-auth instructions:

```python
# In @require_scopes decorator, when scopes are missing:
if missing:
    # Return error with clear instructions for scope upgrade
    raise InsufficientScopeError(
        missing_scopes=list(missing),
        message=(
            f"This action requires additional permissions: {', '.join(missing)}.\n\n"
            f"To authorize these scopes, call nc_auth_update_scopes with:\n"
            f"  additional_scopes={list(missing)}"
        ),
    )
```

**Design Choice**: We use explicit tool calls for re-auth rather than automatic elicitation because:
1. Users should consciously decide to expand access
2. Scope changes are auditable events
3. Avoids unexpected browser redirects during normal operation

### Astrolabe Front-End Integration

**Astrolabe** is the Nextcloud PHP app that provides a management UI for the MCP server. It needs to support Login Flow v2 for users who access MCP via the Nextcloud web interface.

**Integration Points:**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Nextcloud Instance                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      Astrolabe App (/apps/astrolabe)                 │   │
│  │                                                                       │   │
│  │  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐  │   │
│  │  │  MCP Status     │    │  Scope Manager  │    │  Connection     │  │   │
│  │  │  Dashboard      │    │  UI             │    │  Settings       │  │   │
│  │  └────────┬────────┘    └────────┬────────┘    └────────┬────────┘  │   │
│  │           │                      │                      │           │   │
│  │           └──────────────────────┼──────────────────────┘           │   │
│  │                                  │                                   │   │
│  │                          ┌───────▼───────┐                          │   │
│  │                          │ Login Flow v2 │                          │   │
│  │                          │ Controller    │                          │   │
│  │                          └───────┬───────┘                          │   │
│  └──────────────────────────────────┼──────────────────────────────────┘   │
│                                     │                                       │
│  ┌──────────────────────────────────▼──────────────────────────────────┐   │
│  │                    Nextcloud Core (/index.php/login/v2)              │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │ App Password
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           MCP Server                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  POST /api/v1/users/{user_id}/app-password                          │   │
│  │  - Receives app password from Astrolabe                              │   │
│  │  - Stores encrypted with scopes                                      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Astrolabe UI Components:**

1. **Scope Selection UI** (`/apps/astrolabe/src/components/ScopeSelector.vue`):
   ```vue
   <template>
     <div class="scope-selector">
       <h3>Select MCP Access Permissions</h3>
       <p class="description">
         Choose which Nextcloud features the MCP server can access on your behalf.
       </p>

       <div v-for="category in scopeCategories" :key="category.name" class="scope-category">
         <h4>{{ category.label }}</h4>
         <NcCheckboxRadioSwitch
           v-for="scope in category.scopes"
           :key="scope.id"
           v-model="selectedScopes"
           :value="scope.id"
           type="checkbox"
         >
           {{ scope.label }}
           <template #description>{{ scope.description }}</template>
         </NcCheckboxRadioSwitch>
       </div>

       <NcButton @click="initiateLoginFlow" :disabled="selectedScopes.length === 0">
         Authorize Access
       </NcButton>
     </div>
   </template>
   ```

2. **Login Flow Controller** (`/apps/astrolabe/lib/Controller/LoginFlowController.php`):
   ```php
   /**
    * Initiate Login Flow v2 and redirect user to authorization.
    * After completion, store app password in MCP server.
    */
   public function initiateFlow(array $requestedScopes): RedirectResponse {
       // Start Login Flow v2
       $response = $this->httpClient->post(
           $this->urlGenerator->getAbsoluteURL('/index.php/login/v2'),
           ['headers' => ['User-Agent' => 'Astrolabe MCP Provisioning']]
       );

       $data = json_decode($response->getBody(), true);

       // Store session state
       $this->session->set('mcp_login_flow', [
           'poll_endpoint' => $data['poll']['endpoint'],
           'poll_token' => $data['poll']['token'],
           'requested_scopes' => $requestedScopes,
           'expires' => time() + 600,
       ]);

       // Redirect to Nextcloud login
       return new RedirectResponse($data['login']);
   }

   /**
    * Callback after user completes Login Flow.
    * Poll for credentials and send to MCP server.
    */
   public function completeFlow(): JSONResponse {
       $session = $this->session->get('mcp_login_flow');

       // Poll for completion
       $response = $this->httpClient->post($session['poll_endpoint'], [
           'form_params' => ['token' => $session['poll_token']]
       ]);

       if ($response->getStatusCode() === 200) {
           $credentials = json_decode($response->getBody(), true);

           // Send to MCP server
           $this->mcpClient->storeAppPassword(
               userId: $this->userSession->getUser()->getUID(),
               appPassword: $credentials['appPassword'],
               scopes: $session['requested_scopes']
           );

           $this->session->remove('mcp_login_flow');

           return new JSONResponse(['status' => 'success']);
       }

       return new JSONResponse(['status' => 'pending']);
   }
   ```

3. **Current Scopes Display** (`/apps/astrolabe/src/components/CurrentAccess.vue`):
   ```vue
   <template>
     <div class="current-access">
       <h3>Current MCP Access</h3>

       <div v-if="accessStatus.provisioned">
         <p>Access provisioned on {{ formatDate(accessStatus.created_at) }}</p>

         <h4>Authorized Scopes:</h4>
         <ul class="scope-list">
           <li v-for="scope in accessStatus.scopes" :key="scope">
             <NcIconSvgWrapper :path="getScopeIcon(scope)" />
             {{ formatScope(scope) }}
           </li>
         </ul>

         <div class="actions">
           <NcButton @click="showScopeUpdate = true">
             Update Permissions
           </NcButton>
           <NcButton type="error" @click="revokeAccess">
             Revoke Access
           </NcButton>
         </div>
       </div>

       <div v-else>
         <NcEmptyContent>
           <template #icon><AccountIcon /></template>
           <template #description>
             MCP access not configured. Set up access to use AI assistants with your Nextcloud.
           </template>
           <template #action>
             <NcButton @click="showScopeSelector = true">
               Set Up Access
             </NcButton>
           </template>
         </NcEmptyContent>
       </div>
     </div>
   </template>
   ```

**API Endpoints for Astrolabe:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/users/{user_id}/access` | GET | Check provisioning status and scopes |
| `/api/v1/users/{user_id}/app-password` | POST | Store app password with scopes |
| `/api/v1/users/{user_id}/app-password` | DELETE | Revoke access |
| `/api/v1/users/{user_id}/scopes` | PATCH | Update scopes (triggers re-auth) |
| `/api/v1/scopes` | GET | List all supported scopes with descriptions |

### Database Schema Changes

Add `scopes` column to `app_passwords` table and new `login_flow_sessions` table:

```sql
-- Migration: 003_add_scopes_and_login_flow_sessions.py

-- Add scopes column to existing app_passwords table (JSON array)
ALTER TABLE app_passwords ADD COLUMN scopes TEXT;

-- Add login flow sessions table for pending authorizations
CREATE TABLE IF NOT EXISTS login_flow_sessions (
    user_id TEXT PRIMARY KEY,
    poll_token TEXT NOT NULL,
    poll_endpoint TEXT NOT NULL,
    requested_scopes TEXT NOT NULL,  -- JSON array
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

-- Create index for cleanup of expired sessions
CREATE INDEX IF NOT EXISTS idx_login_flow_expires
ON login_flow_sessions(expires_at);
```

**Updated app_passwords schema:**
```sql
CREATE TABLE app_passwords (
    user_id TEXT PRIMARY KEY,
    encrypted_password BLOB NOT NULL,
    username TEXT NOT NULL,         -- Nextcloud login name
    scopes TEXT,                    -- JSON array of authorized scopes (NEW)
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

### Scope Enforcement in @require_scopes Decorator

Modify the decorator to check scopes from stored app password when OAuth token is not available:

```python
def require_scopes(*required_scopes: str):
    """
    Decorator to require specific scopes for MCP tool execution.

    Scope enforcement modes:
    1. OAuth mode (access_token present): Check token scopes
    2. App password mode (no token, stored app password): Check stored scopes
    3. Single-user mode (env var app password): Bypass checks (trusted environment)
    """

    def decorator(func: Callable) -> Callable:
        func._required_scopes = list(required_scopes)
        func_name = getattr(func, "__name__", repr(func))
        context_param_name = find_context_parameter(func)

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx: Context | None = (
                kwargs.get(context_param_name) if context_param_name else None
            )

            if ctx is None:
                # No context - allow (BasicAuth mode, backwards compat)
                logger.debug(f"No context for {func_name} - allowing")
                return await func(*args, **kwargs)

            # Try OAuth token first
            access_token: AccessToken | None = getattr(
                ctx.request_context, "access_token", None
            )

            if access_token is not None:
                # OAuth mode: check token scopes (existing logic)
                return await _check_oauth_scopes(
                    func, access_token, required_scopes, *args, **kwargs
                )

            # No OAuth token - check deployment mode
            settings = get_settings()

            if settings.nextcloud_app_password:
                # Single-user mode with env var: bypass scope checks
                logger.debug(f"Single-user mode for {func_name} - allowing")
                return await func(*args, **kwargs)

            # Multi-user mode: check stored app password scopes
            user_id = extract_user_from_context(ctx)
            if user_id is None:
                raise ScopeAuthorizationError("Cannot determine user identity")

            storage = get_storage()
            app_password_data = await storage.get_app_password_with_scopes(user_id)

            if app_password_data is None:
                raise ProvisioningRequiredError(
                    "Nextcloud access not provisioned. "
                    "Call nc_auth_provision_access to authorize."
                )

            stored_scopes = set(app_password_data.get("scopes") or [])
            required_set = set(required_scopes)
            missing = required_set - stored_scopes

            if missing:
                # Log scope mismatch for audit
                await _audit_scope_mismatch(user_id, func_name, missing, stored_scopes)

                raise InsufficientScopeError(
                    list(missing),
                    f"Access denied to {func_name}: Missing scopes {missing}. "
                    f"Re-provision with nc_auth_provision_access to request additional scopes."
                )

            logger.debug(f"App password scope check passed for {func_name}")
            return await func(*args, **kwargs)

        return wrapper
    return decorator
```

### Configuration Validation Simplification

Replace 5 modes with 2 modes:

```python
class AuthMode(Enum):
    SINGLE_USER = "single_user"
    MULTI_USER = "multi_user"


MODE_REQUIREMENTS: dict[AuthMode, ModeRequirements] = {
    AuthMode.SINGLE_USER: ModeRequirements(
        required=["nextcloud_host", "nextcloud_app_password"],
        optional=[
            "nextcloud_username",  # Inferred from app password if not set
            "enable_semantic_search",
            "qdrant_url",
            "qdrant_location",
        ],
        forbidden=[],
        conditional={
            "enable_semantic_search": ["qdrant_url OR qdrant_location"],
        },
        description="Single-user deployment with app password in environment. "
                   "Suitable for personal instances and development.",
    ),
    AuthMode.MULTI_USER: ModeRequirements(
        required=["nextcloud_host", "token_encryption_key", "token_storage_db"],
        optional=[
            "enable_semantic_search",
            "qdrant_url",
            "qdrant_location",
        ],
        forbidden=["nextcloud_app_password"],
        conditional={
            "enable_semantic_search": ["qdrant_url OR qdrant_location"],
        },
        description="Multi-user deployment with per-user app passwords via Login Flow v2. "
                   "App passwords acquired through browser-based authorization.",
    ),
}
```

## Security Considerations

### Critical: Application-Level Scope Enforcement

**Nextcloud app passwords have NO native scope support.** They grant full API access equivalent to the user's permissions in Nextcloud.

**Implications:**
1. The MCP server enforces scopes at the application level only
2. A compromised MCP server could bypass scope restrictions
3. A malicious actor with direct access to stored app passwords has full Nextcloud API access

**Mitigations:**
1. **Clear Documentation**: Administrators must understand this trust model
2. **Audit Logging**: Log all scope enforcement decisions for security review
3. **Encryption at Rest**: App passwords encrypted with Fernet (AES-256)
4. **User Visibility**: App passwords visible in Nextcloud Settings > Security > Devices & Sessions
5. **User Revocation**: Users can revoke app passwords directly in Nextcloud
6. **Named App Passwords**: User-Agent includes user ID for identification

### Security Posture Documentation

Include this warning in deployment documentation and server startup logs:

> **Security Notice: Scope Enforcement Limitations**
>
> App passwords generated via Login Flow v2 grant full API access to Nextcloud
> at the Nextcloud level. The MCP server enforces scope restrictions at the
> application level only.
>
> **What this means:**
> - When a user authorizes scopes like `notes:read`, the MCP server records
>   these scopes and enforces them before executing tools
> - The underlying app password can access ANY Nextcloud API the user can access
> - Scope enforcement is defense-in-depth, not a security boundary
>
> **Trust Model:**
> - Trust the MCP server to enforce scopes correctly
> - Trust the MCP server's storage to be secure (encrypted, access-controlled)
> - Users can revoke access via Nextcloud Settings > Security > Devices & Sessions
>
> **Audit Trail:**
> - All scope enforcement decisions are logged
> - Scope denials include user ID, tool name, and missing scopes
> - Logs can be forwarded to SIEM for security monitoring

### Rate Limiting

Rate limiting is **configurable** and should be tuned based on deployment size and usage patterns. The MCP server is an external client to Nextcloud and should not require special Nextcloud configuration to function.

**Configuration Variables:**

```bash
# Login Flow v2 rate limits (environment variables)
LOGIN_FLOW_INITIATE_LIMIT=5      # Max initiations per user per window (default: 5)
LOGIN_FLOW_INITIATE_WINDOW=3600  # Window in seconds (default: 1 hour)
LOGIN_FLOW_POLL_INTERVAL=10      # Seconds between poll attempts (default: 10)
LOGIN_FLOW_POLL_TIMEOUT=600      # Max seconds to poll before timeout (default: 10 min)
```

**Implementation:**

```python
async def nc_auth_provision_access(ctx: Context, ...) -> ProvisionAccessResponse:
    user_id = extract_user_from_mcp_token(ctx)
    settings = get_settings()

    # Rate limit check for initiation (configurable)
    if await is_rate_limited(
        user_id,
        "login_flow_initiate",
        limit=settings.login_flow_initiate_limit,
        window=settings.login_flow_initiate_window,
    ):
        raise RateLimitError("Too many provisioning attempts. Try again later.")

    await record_rate_limit_hit(user_id, "login_flow_initiate")
    # ... rest of implementation
```

**Administrator Guidance:**

| Deployment Size | Recommended Initiate Limit | Notes |
|-----------------|---------------------------|-------|
| Personal (1-5 users) | 10/hour | Higher limit acceptable |
| Small team (5-50 users) | 5/hour | Default is appropriate |
| Enterprise (50+ users) | 3/hour | Consider integration with external rate limiting |

Rate limiting at the MCP server level is defense-in-depth. Administrators should also consider:
- Nextcloud's built-in brute force protection
- Reverse proxy rate limiting (nginx, Traefik)
- Network-level controls for multi-user deployments

### Audit Logging

All authentication and scope-related events are logged:

```python
AUDIT_EVENTS = [
    "login_flow_initiated",      # User started provisioning
    "login_flow_completed",      # User completed provisioning
    "login_flow_failed",         # Provisioning failed (timeout, rejection)
    "login_flow_expired",        # Session expired before completion
    "scope_enforcement_allowed", # Tool execution allowed
    "scope_enforcement_denied",  # Tool execution denied (missing scopes)
    "app_password_stored",       # App password saved
    "app_password_deleted",      # App password revoked
    "app_password_used",         # App password used for API call
]
```

## Migration Path

### Modes Being Removed

| Current Mode | Replacement | Deprecation Reason |
|--------------|-------------|-------------------|
| Single-User BasicAuth | Mode 1 (Single-User) | Renamed only (`NEXTCLOUD_PASSWORD` → `NEXTCLOUD_APP_PASSWORD`) |
| Multi-User BasicAuth | Mode 2 (Multi-User) | Credential pass-through is a security anti-pattern |
| OAuth Single-Audience | Mode 2 (Multi-User) | Requires upstream Nextcloud patches not planned for adoption |
| OAuth Token Exchange | Mode 2 (Multi-User) | Complex IdP configuration, limited adoption |
| Smithery Stateless | **DROPPED** | Third-party hosting conflicts with privacy goals; use self-hosted alternatives |

### Phase 1: Add Login Flow v2 Support (v0.65)

- Implement `nc_auth_provision_access` and `nc_auth_check_status` tools
- Add `scopes` column to `app_passwords` table
- Add `login_flow_sessions` table
- Update `@require_scopes` decorator for app password mode
- Mark OAuth modes as deprecated in documentation
- Log deprecation warnings when deprecated modes detected

### Phase 2: Deprecation Period (v0.66)

- Add prominent deprecation warnings at startup
- Provide migration guide with step-by-step instructions
- Add tooling to help users transition (config checker, etc.)
- Continue supporting all modes with warnings

### Phase 3: Remove Deprecated Modes (v1.0)

- Remove `ENABLE_TOKEN_EXCHANGE`, `ENABLE_MULTI_USER_BASIC_AUTH`, `SMITHERY_*` variables
- Remove OAuth token pass-through code paths
- Remove Smithery stateless mode
- Simplify configuration validation to 2 modes
- Update all documentation

### Backward Compatibility During Transition

Existing configurations continue working during the transition period:

```python
# Config detection during transition
def detect_deployment_mode() -> AuthMode:
    settings = get_settings()

    # Explicit mode takes precedence
    if settings.mcp_deployment_mode:
        return settings.mcp_deployment_mode

    # Legacy mode detection with deprecation warnings
    if settings.enable_token_exchange:
        logger.warning(
            "ENABLE_TOKEN_EXCHANGE is deprecated. "
            "Migrate to Multi-User mode with Login Flow v2. "
            "See: https://docs.example.com/migration"
        )
        return AuthMode.MULTI_USER  # Treat as multi-user

    if settings.enable_multi_user_basic_auth:
        logger.warning(
            "ENABLE_MULTI_USER_BASIC_AUTH is deprecated. "
            "Migrate to Multi-User mode with Login Flow v2."
        )
        return AuthMode.MULTI_USER

    if settings.nextcloud_app_password:
        return AuthMode.SINGLE_USER

    # No app password configured = multi-user mode
    return AuthMode.MULTI_USER
```

## Consequences

### Positive

1. **Simpler Deployment**: 2 modes instead of 5
2. **No Upstream Dependencies**: Works on any Nextcloud 16+ without patches
3. **Better UX**: Browser-based authorization (familiar pattern for users)
4. **User Control**: App passwords visible and revocable in Nextcloud settings
5. **Reduced Maintenance**: Less configuration validation code
6. **Standard Pattern**: Login Flow v2 is the same mechanism used by all official Nextcloud clients
7. **Clearer Security Model**: Application-level scope enforcement is explicit, not hidden
8. **Audit Trail**: All scope decisions logged for security review
9. **Seamless Elicitation**: MCP clients automatically prompt for authorization when needed
10. **Progressive Scope Grants**: Users can start with minimal scopes and add more as needed
11. **Dual Entry Points**: Both MCP clients (via elicitation) and Astrolabe UI can initiate provisioning

### Negative

1. **Scope Enforcement at Application Level**: Not enforced by Nextcloud itself (platform limitation)
2. **Trust in MCP Server**: Administrators must trust server to enforce scopes correctly
3. **Migration Effort**: Existing OAuth deployments need users to re-provision
4. **No Fine-Grained Nextcloud Permissions**: App passwords grant full user-level access
5. **No Third-Party Hosted Option**: Users requiring managed hosting must self-host

### Neutral

1. **Same Security Model as Desktop/Mobile Apps**: App passwords are already the standard for Nextcloud clients
2. **Background Sync Unchanged**: App passwords work for offline operations
3. **Testing Simplified**: Fewer containers and configurations to maintain

## Alternatives Considered

### Alternative 1: Keep All OAuth Modes

**Rejected**: Maintains complexity, requires upstream patches, limited adoption due to IdP configuration requirements. The current OAuth modes require either:
- Patched `user_oidc` app for Bearer token validation on non-OCS endpoints
- Complex multi-IdP configuration for token exchange

### Alternative 2: Remove Scope Support Entirely

**Rejected**: Security regression. Even application-level enforcement provides:
- Defense-in-depth against accidental misuse
- Audit logging for security review
- User-visible scope grants for transparency
- Foundation for future Nextcloud-native scope support

### Alternative 3: Use Nextcloud's Native OAuth

**Rejected**: Nextcloud's OAuth implementation doesn't support fine-grained scopes. The Notes/Calendar/WebDAV APIs don't check OAuth scopes - they only verify the token is valid. This means Nextcloud OAuth provides no additional security over app passwords.

### Alternative 4: Implement Scope Support in Nextcloud Upstream

**Considered for Future**: Contributing scope enforcement upstream would be the ideal long-term solution. However:
- Significant upstream contribution effort
- Requires changes to multiple Nextcloud apps
- Doesn't solve immediate consolidation needs
- Can be pursued in parallel without blocking this ADR

### Alternative 5: Keep Smithery/Third-Party Hosted Mode

**Rejected**: The MCP server prioritizes privacy and user data control. Third-party hosted deployments introduce trust and data residency concerns that conflict with these goals.

**Recommendation for users:**
- **Individual users**: Use Single-User mode with self-hosted deployment
- **Organizations**: Use Multi-User mode with organizational infrastructure

Users who require managed hosting should evaluate the privacy implications and consider self-hosting alternatives (Docker, Kubernetes, VM-based deployments).

### Alternative 6: Wait for Nextcloud OAuth Bearer Token Support

**Rejected for now, with future revisit planned**: Nextcloud does not currently support scoped OAuth bearer token validation on most App APIs. The current OAuth implementation validates tokens but does not enforce scopes at the API level.

**Current state:**
- WebDAV and OCS endpoints accept OAuth bearer tokens (but without scope enforcement)
- CalDAV, CardDAV, Notes API, and other App APIs do not accept OAuth bearer tokens
- No upstream plans announced to add scoped OAuth support

**Our approach:**
- Keep the implementation simple using Login Flow v2 and app passwords
- Application-level scope enforcement provides defense-in-depth
- If Nextcloud adds scoped OAuth support for App APIs in the future, we will revisit this architecture to leverage native scope enforcement

This approach prioritizes simplicity and compatibility over waiting for uncertain upstream changes.

## References

- [Nextcloud Login Flow Documentation](https://docs.nextcloud.com/server/latest/developer_manual/client_apis/LoginFlow/index.html)
- [MCP Specification - Elicitation](https://spec.modelcontextprotocol.io/specification/2025-11-25/server/elicitation/) (revision 2025-11-25)
- [ADR-020: Deployment Modes and Configuration Validation](ADR-020-deployment-modes-and-configuration-validation.md)
- [ADR-021: Configuration Consolidation](ADR-021-configuration-consolidation.md)
- [ADR-004: Progressive Consent OAuth Architecture](ADR-004-mcp-application-oauth.md)
- [GitHub Issue #521: Login Flow v2 Support](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/521)

## Implementation Checklist

### Phase 1: MCP Server Core (Login Flow v2)

| File | Changes |
|------|---------|
| `nextcloud_mcp_server/auth/scope_authorization.py` | Add app password scope checking, elicitation support |
| `nextcloud_mcp_server/auth/storage.py` | Add `scopes` field, `login_flow_sessions` methods |
| `nextcloud_mcp_server/server/auth_tools.py` | Add `nc_auth_provision_access`, `nc_auth_check_status`, `nc_auth_update_scopes` |
| `nextcloud_mcp_server/auth/login_flow.py` | New: Login Flow v2 client implementation |
| `nextcloud_mcp_server/auth/elicitation.py` | New: MCP elicitation helpers for URL opening |
| `nextcloud_mcp_server/config.py` | Simplify mode detection to 2 modes |
| `nextcloud_mcp_server/config_validators.py` | Reduce validation to 2 modes |
| `alembic/versions/` | Migration for `scopes` column and `login_flow_sessions` table |

### Phase 2: Astrolabe Front-End

| File | Changes |
|------|---------|
| `astrolabe/lib/Controller/LoginFlowController.php` | New: PHP controller for Login Flow v2 |
| `astrolabe/lib/Service/McpClientService.php` | Add scope storage API calls |
| `astrolabe/src/components/ScopeSelector.vue` | New: Scope selection UI |
| `astrolabe/src/components/CurrentAccess.vue` | New: Current access status and management |
| `astrolabe/src/views/Settings.vue` | Integrate Login Flow v2 UI |

### Phase 3: API Endpoints

| Endpoint | File | Purpose |
|----------|------|---------|
| `GET /api/v1/users/{user_id}/access` | `nextcloud_mcp_server/api/access.py` | Check provisioning status |
| `POST /api/v1/users/{user_id}/app-password` | `nextcloud_mcp_server/api/passwords.py` | Store app password (existing, add scopes) |
| `PATCH /api/v1/users/{user_id}/scopes` | `nextcloud_mcp_server/api/access.py` | Update scopes (trigger re-auth) |
| `GET /api/v1/scopes` | `nextcloud_mcp_server/api/access.py` | List supported scopes with descriptions |

### Phase 4: Documentation (Required)

| File | Changes |
|------|---------|
| `README.md` | Add security notice about Nextcloud scope limitation (see below) |
| `docs/authentication.md` | Rewrite for 2-mode architecture |
| `docs/configuration.md` | Simplify configuration docs, add rate limiting guidance |
| `docs/astrolabe-integration.md` | New: Astrolabe setup guide |
| `docs/security-posture.md` | New: Security model documentation for admins |

**Required README Addition:**

```markdown
## Security Notice: Scope Enforcement

> **Important**: Nextcloud does not support scoped app passwords or OAuth scopes for
> most App APIs. This is a Nextcloud platform limitation, not an MCP server limitation.
>
> The MCP server implements **application-level scope enforcement** as a defense-in-depth
> measure. When users authorize scopes like `notes:read`, the MCP server records and
> enforces these scopes before executing tools. However, the underlying app password
> can access any Nextcloud API the user has permission to access.
>
> **Administrators should understand:**
> - Scope enforcement occurs at the MCP server layer, not the Nextcloud layer
> - A compromised MCP server could bypass scope restrictions
> - Users can revoke access via Nextcloud Settings > Security > Devices & Sessions
>
> If Nextcloud adds scoped OAuth support for App APIs in the future, this architecture
> will be revisited to leverage native scope enforcement.
```

### Phase 5: Recommended Nextcloud Configuration

Document recommended Nextcloud settings for optimal MCP server operation:

| Setting | Recommendation | Purpose |
|---------|---------------|---------|
| `'auth.bruteforce.protection.enabled'` | `true` (default) | Protects Login Flow v2 from abuse |
| `'ratelimit.protection.enabled'` | `true` (default) | General API rate limiting |
| `'trusted_proxies'` | Configure if behind reverse proxy | Accurate IP detection for rate limiting |

**Note**: The MCP server is designed to work with a standard Nextcloud deployment without special configuration. These are recommendations for production deployments.

### Verification Steps (Required for Implementation)

All tests must pass before the feature is considered complete.

**Unit Tests:**
1. `@require_scopes` decorator with app password scopes (no OAuth token)
2. MCP elicitation response generation with capability detection
3. Elicitation fallback when URL mode not supported
4. Scope merging logic for re-authentication
5. Rate limiting configuration validation

**Integration Tests:**
6. Login Flow v2 initiation, polling, and completion
7. Re-auth flow for scope updates
8. Scope enforcement denies unauthorized access
9. Scope merging preserves existing scopes on re-auth

**End-to-End Tests:**
10. MCP client → provisioning → Login Flow v2 → Nextcloud API call
11. Astrolabe UI → Login Flow v2 → MCP server storage

**Migration Tests:**
12. Existing OAuth deployment transitions to new mode with deprecation warnings

**Security Tests:**
13. Verify scope enforcement cannot be bypassed via direct API calls
14. Verify app password encryption and secure storage
15. Verify rate limiting prevents abuse
