# ADR-018: Nextcloud PHP App for Settings and Management UI

**Status**: Proposed
**Date**: 2025-12-14
**Updated**: 2025-12-15 (Added deployment modes and authentication architecture)
**Related**: ADR-011 (AppAPI Architecture - Rejected), ADR-008 (MCP Sampling), ADR-004 (OAuth Progressive Consent)

## Context

The Nextcloud MCP Server currently provides a browser-based administrative interface at the `/app` endpoint, implemented as part of the standalone MCP server using Starlette routing. This interface provides:

- User information and session management
- Vector sync status monitoring with real-time updates
- Interactive vector visualization with 2D PCA plots
- Webhook management (admin only)
- OAuth login/logout flows

While this approach works functionally, it has several limitations:

### Current Architecture Limitations

**1. Separate Authentication System**
- Users must authenticate separately to access `/app` endpoint
- Browser OAuth flow creates session cookies independent of Nextcloud
- No integration with Nextcloud's existing user sessions
- Duplicates authentication logic that Nextcloud already provides

**2. Deployment Complexity**
- `/app` endpoint must be exposed alongside MCP protocol endpoints
- Requires separate routing, templates, static file serving in MCP server
- Mixing concerns: MCP protocol handler + web UI in same codebase
- Users must bookmark/remember separate URL (e.g., `mcp-server.example.com/app`)

**3. Limited Integration**
- Cannot appear in Nextcloud's settings interface
- No integration with Nextcloud's design system
- Missing Nextcloud features: notifications, activity stream, search
- Doesn't follow Nextcloud UX patterns users are familiar with

**4. Mobile and Accessibility**
- Must implement responsive design separately
- Accessibility features reimplemented instead of using NC's framework
- No integration with Nextcloud mobile apps

**5. Maintenance Burden**
- Must maintain HTML templates, CSS, JavaScript in Python codebase
- Jinja2 templating separate from Nextcloud's template system
- Static file serving and caching handled manually
- HTMX and Alpine.js dependencies managed separately

### Why Not ExApp Architecture?

In ADR-011, we extensively investigated running the MCP server as a Nextcloud ExApp (External Application). This would have provided native Nextcloud integration but was **rejected due to fundamental protocol incompatibilities**:

**Critical Limitations of ExApp Architecture:**
- ❌ **No MCP sampling** - AppAPI proxy blocks bidirectional communication required for RAG
- ❌ **No real-time progress updates** - Stateless request/response proxy prevents server→client notifications
- ❌ **Buffered-only streaming** - ExApp proxy accumulates responses, preventing incremental updates
- ❌ **No persistent connections** - MCP protocol features like elicitation impossible

**Validation from Nextcloud's Own Projects:**
- Nextcloud's Context Agent ExApp faces identical limitations
- Works around them by using Task Processing API instead of MCP protocol
- Confirms limitations are architectural, not implementation-specific

**Conclusion from ADR-011:**
> The hybrid OAuth + AppAPI architecture is not viable for this project's use case. While AppAPI ExApps provide value for in-app Nextcloud integration, the architectural constraints fundamentally conflict with MCP's protocol requirements for external client integration.

**Therefore:** MCP server must remain standalone with OAuth mode to support full MCP protocol capabilities.

### The Solution: Nextcloud PHP App for UI Only

Instead of running the MCP server as an ExApp (which breaks the protocol), we can create a **lightweight Nextcloud PHP app that provides only the UI** while the MCP server remains standalone:

**Key Insight:** The UI doesn't need to be in the same process as the MCP protocol handler. We can separate concerns:
- **MCP Server (Python)**: Protocol handling, background workers, vector sync, sampling support
- **Nextcloud PHP App**: UI only, delegates all operations to MCP server via management API

This gives us **native Nextcloud integration without the ExApp protocol limitations**.

## Decision

We will **migrate the `/app` administrative interface to a standalone Nextcloud PHP app** while keeping the MCP server as a standalone service with OAuth mode.

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  Nextcloud PHP App (UI Only)                                │
│  ├─ OAuth Client (PKCE flow via NC OIDC)                    │
│  ├─ Personal Settings Panel                                 │
│  ├─ Admin Settings Panel                                    │
│  ├─ Vector Visualization Page (Vue.js)                      │
│  ├─ Webhook Management (admins)                             │
│  └─ Session Management (revoke access)                      │
└──────────────────────┬──────────────────────────────────────┘
                       │ (Management API - HTTP REST)
                       │ - Authentication: OAuth Bearer Token
                       │ - Same token audience as MCP clients
                       │ - Token validated by UnifiedTokenVerifier
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Standalone MCP Server (OAuth Mode)                         │
│  ├─ /mcp/* - MCP Protocol Endpoints (FastMCP)              │
│  │   └─ Full sampling/elicitation support                   │
│  ├─ /api/v1/* - Management API (NEW)                        │
│  │   ├─ /status - Server health, version                    │
│  │   ├─ /users/{id}/session - User session details          │
│  │   ├─ /users/{id}/revoke - Revoke background access       │
│  │   ├─ /vector-sync/status - Indexing metrics              │
│  │   └─ /vector-viz/search - Search API for visualization   │
│  ├─ OAuth Endpoints (existing)                              │
│  │   ├─ /oauth/authorize - Client authorization             │
│  │   ├─ /oauth/callback - OAuth callback                    │
│  │   └─ /oauth/token - Token endpoint                       │
│  └─ Background Workers                                      │
│      ├─ Vector sync scanner                                 │
│      └─ Webhook processors                                  │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
               Nextcloud APIs
               (Notes, Calendar, Files, etc.)
```

### Communication Flow

**PHP App OAuth Flow:**
```
1. User visits NC Personal Settings → PHP App detects no MCP token
2. PHP App redirects to NC OIDC → User authorizes PHP app
3. NC OIDC issues JWT with aud="http://localhost:8001" (MCP server)
4. PHP App stores token, redirects back to settings
5. PHP App calls MCP Management API with Bearer token
6. MCP Server validates token (same verifier as MCP clients)
```

**User Views Settings:**
```
User → NC Web UI → PHP App → Management API → MCP Server
                             (GET /api/v1/status)
                             Authorization: Bearer <oauth_token>
```

**User Revokes Access:**
```
User → NC Web UI → PHP App → Management API → MCP Server
    (click revoke)           (POST /api/v1/users/{id}/revoke)
                             Authorization: Bearer <oauth_token>
                             → Delete refresh token
```

**User Tests Vector Search:**
```
User → NC Web UI → PHP App → Management API → MCP Server
    (enter query)            (POST /api/v1/vector-viz/search)
                             → Execute hybrid search
                             → Return results + PCA coordinates
```

**MCP Client Uses Server:**
```
Claude Desktop → MCP Server /mcp/sse endpoint
                 ↓
                 Full MCP protocol with sampling ✅
                 (Same token audience: MCP server URL)
```

### Core Principles

1. **Separation of Concerns**
   - MCP server handles protocol, background jobs, vector operations
   - PHP app handles UI rendering and user interaction
   - Clear API boundary with versioned REST endpoints

2. **Single Source of Truth**
   - MCP server owns all business logic and state
   - PHP app is stateless, delegates to management API
   - No duplication of authentication, authorization, or data processing

3. **Native Nextcloud Integration**
   - Follows NC settings panel conventions
   - Uses NC design system and components
   - Integrates with NC session management
   - Appears in standard NC settings navigation

4. **Backwards Compatibility**
   - Existing `/app` endpoint remains during migration
   - Users can choose which UI to use
   - Deprecated in Release N, removed in Release N+2

5. **MCP Protocol Integrity**
   - No changes to MCP server architecture (remains OAuth standalone)
   - Full sampling, elicitation, streaming support preserved
   - External MCP clients unaffected

## Implementation Details

### Deployment Modes and Authentication Architecture

The MCP server supports three deployment modes, each with different authentication requirements for the three critical communication paths:

1. **External MCP Client → MCP Server** (e.g., Claude Desktop)
2. **Astrolabe UI → MCP Server** (PHP app REST API calls)
3. **MCP Server → Nextcloud** (background jobs, vector sync)

#### Mode 1: Basic Single-User (Development/Simple Deployments)

**Configuration:**
```bash
DEPLOYMENT_MODE=basic
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=admin_password
```

**Authentication Flows:**

| Communication Path | Method |
|-------------------|--------|
| MCP Client → Server | None (assumes single user) |
| Astrolabe UI → Server | None (uses env credentials) |
| Server → Nextcloud | BasicAuth from environment |

**Use Cases:**
- ✅ Local development
- ✅ Single-user personal deployments
- ✅ Quick start / proof-of-concept

**Limitations:**
- ❌ All clients share same identity
- ❌ No per-user access control
- ❌ Not suitable for multi-user environments

#### Mode 2: Basic Multi-User Pass-Through (Multi-User Without OIDC)

**Configuration:**
```bash
DEPLOYMENT_MODE=basic_multiuser
# No credentials in env - clients provide their own
```

**Authentication Flows:**

| Communication Path | Method |
|-------------------|--------|
| MCP Client → Server | BasicAuth with Nextcloud app password |
| Astrolabe UI → Server | BasicAuth with Nextcloud app password |
| Server → Nextcloud | Pass-through client's credentials |

**Architecture:**

```
┌─────────────────────────────────────────────────────┐
│  MCP Client (Claude Desktop)                        │
│  Config: username=alice, password=<app_password>    │
└──────────────────────┬──────────────────────────────┘
                       │ Authorization: Basic base64(alice:app_pass)
                       ▼
┌─────────────────────────────────────────────────────┐
│  MCP Server (Pass-Through Mode)                     │
│  ├─ Extract BasicAuth credentials from header       │
│  ├─ NO token validation/exchange                    │
│  ├─ Store credentials in request context            │
│  └─ Create NextcloudClient with user's credentials  │
└──────────────────────┬──────────────────────────────┘
                       │ Authorization: Basic base64(alice:app_pass)
                       │ (same credentials forwarded)
                       ▼
                  Nextcloud APIs
                  (validates app password on each request)
```

**Implementation:**

```python
# nextcloud_mcp_server/context.py

async def _get_basic_multiuser_client(ctx: Context) -> NextcloudClient:
    """Create client using BasicAuth credentials from request context.

    In BasicAuth multi-user mode:
    1. Credentials extracted from Authorization header by middleware
    2. Stored in ctx.request_context["basic_auth"]
    3. Used to create NextcloudClient for this request
    4. Nextcloud validates credentials on each API call
    """
    basic_auth = ctx.request_context.get("basic_auth")
    if not basic_auth:
        raise ValueError("BasicAuth credentials not found in context")

    username = basic_auth.get("username")
    password = basic_auth.get("password")

    if not username or not password:
        raise ValueError("Invalid BasicAuth credentials")

    # Create client with user's credentials
    http_client = get_http_client(ctx)
    settings = get_settings()

    return NextcloudClient(
        http_client=http_client,
        host=settings.nextcloud_host,
        username=username,
        basic_auth=(username, password),  # Forwarded to all NC API calls
    )


# nextcloud_mcp_server/app.py - BasicAuth extraction middleware

class BasicAuthMiddleware:
    """Extract BasicAuth credentials and store in request context."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: StarletteScope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()

            if auth_header.startswith("Basic "):
                try:
                    encoded = auth_header[6:]
                    decoded = base64.b64decode(encoded).decode('utf-8')
                    username, password = decoded.split(':', 1)

                    # Store in scope for MCP context
                    scope.setdefault("state", {})
                    scope["state"]["basic_auth"] = {
                        "username": username,
                        "password": password,
                    }
                except Exception as e:
                    logger.warning(f"Failed to parse BasicAuth: {e}")

        await self.app(scope, receive, send)
```

**User Experience:**

Users generate app passwords in Nextcloud settings:
1. Settings → Security → Devices & sessions
2. Create new app password: "Claude Desktop"
3. Copy generated password
4. Configure MCP client:

```json
{
  "mcpServers": {
    "nextcloud": {
      "url": "https://mcp.example.com/mcp",
      "auth": {
        "type": "basic",
        "username": "alice",
        "password": "xxxx-xxxx-xxxx-xxxx"
      }
    }
  }
}
```

**Astrolabe UI in BasicAuth Multi-User:**

The PHP app prompts users to save their app password:

```php
// lib/Service/McpServerClient.php

public function search(string $query, array $options = []): array {
    $user = $this->userSession->getUser();
    $userId = $user->getUID();

    // Get user's app password from settings
    $appPassword = $this->config->getUserValue(
        $userId, 'astrolabe', 'app_password', ''
    );

    if (empty($appPassword)) {
        return ['error' => 'Please configure app password in Astrolabe settings'];
    }

    $response = $this->httpClient->post(
        $this->baseUrl . '/api/v1/vector-viz/search',
        [
            'json' => ['query' => $query] + $options,
            'auth' => [$userId, $appPassword],  // BasicAuth
        ]
    );

    return json_decode($response->getBody(), true);
}
```

**Use Cases:**
- ✅ Multi-user deployments
- ✅ No OIDC infrastructure required
- ✅ Each user has independent identity
- ✅ App passwords revocable per-device
- ✅ Background jobs work (same credentials)

**Security Considerations:**

| Aspect | BasicAuth Multi-User | OAuth (Mode 3) |
|--------|---------------------|----------------|
| **Credential scope** | ⚠️ Full NC access | ✅ Token audience-scoped |
| **Credential lifetime** | ⚠️ Until manually revoked | ✅ Short-lived access tokens |
| **MCP server sees** | ⚠️ Password in plaintext | ✅ Only opaque tokens |
| **Credential exposure** | ⚠️ Higher risk | ✅ Lower risk |
| **Setup complexity** | ✅ Simple | ⚠️ Requires OIDC |

**Required Security Measures:**

```python
# nextcloud_mcp_server/app.py

if deployment_mode == DeploymentMode.BASIC_MULTIUSER:
    # Require TLS in production
    if not settings.allow_insecure_transport:
        if not settings.server_url.startswith("https://"):
            raise ValueError(
                "BasicAuth multi-user mode requires HTTPS. "
                "Set ALLOW_INSECURE_TRANSPORT=true for testing only."
            )

    # Never log credentials
    logging.getLogger("httpx").setLevel(logging.WARNING)

    logger.warning(
        "BasicAuth multi-user mode: credentials passed through to Nextcloud. "
        "Consider OAuth mode for production deployments."
    )
```

**Limitations:**
- ⚠️ Credentials not audience-scoped (full NC access)
- ⚠️ Credentials transmitted to MCP server (trust required)
- ⚠️ No token refresh mechanism
- ⚠️ Requires HTTPS for security

#### Mode 3: OAuth (Production Multi-User)

**Configuration:**
```bash
DEPLOYMENT_MODE=oauth
OIDC_ISSUER=https://keycloak.example.com/realms/nextcloud
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_OFFLINE_ACCESS=true  # For background jobs
```

**Authentication Flows:**

| Communication Path | Method |
|-------------------|--------|
| MCP Client → Server | OAuth Bearer token (PKCE flow) |
| Astrolabe UI → Server | OAuth Bearer token (PKCE flow) |
| Server → Nextcloud | Token exchange OR refresh token (Flow 2) |

**Architecture:**

```
┌─────────────────────────────────────────────────────┐
│  MCP Client / Astrolabe UI                         │
└──────────────────────┬──────────────────────────────┘
                       │ PKCE OAuth Flow
                       ▼
┌─────────────────────────────────────────────────────┐
│  Identity Provider (Keycloak/NextcloudOIDC)         │
│  Issues: audience-scoped access token               │
└──────────────────────┬──────────────────────────────┘
                       │ Authorization: Bearer <token>
                       ▼
┌─────────────────────────────────────────────────────┐
│  MCP Server                                          │
│  ├─ Validates token (UnifiedTokenVerifier)          │
│  ├─ Checks audience (must match server URL)         │
│  ├─ Exchanges token for NC token OR                 │
│  └─ Uses refresh token (if Flow 2 completed)        │
└──────────────────────┬──────────────────────────────┘
                       │ Authorization: Bearer <nc_token>
                       ▼
                  Nextcloud APIs
```

**Use Cases:**
- ✅ Production deployments
- ✅ Security-sensitive environments
- ✅ Background jobs requiring offline access
- ✅ External MCP clients with advanced features
- ✅ Token audience scoping required

**Benefits:**
- ✅ Short-lived access tokens
- ✅ Token audience validation
- ✅ Refresh tokens for background jobs
- ✅ MCP sampling support
- ✅ Credential separation (MCP server never sees user passwords)

**Limitations:**
- ⚠️ Requires OIDC infrastructure
- ⚠️ More complex setup
- ⚠️ Progressive consent flow needed for offline access

See ADR-004 for complete OAuth architecture details.

#### Deployment Mode Decision Matrix

Choose deployment mode based on requirements:

| Requirement | Basic Single | Basic Multi-User | OAuth |
|-------------|--------------|------------------|-------|
| **Multi-user support** | ❌ | ✅ | ✅ |
| **Per-user identity** | ❌ | ✅ | ✅ |
| **External MCP clients** | ⚠️ No auth | ✅ BasicAuth | ✅ OAuth |
| **Astrolabe UI access** | ✅ | ✅ | ✅ |
| **Background jobs** | ✅ | ✅ | ✅ |
| **OIDC required** | ❌ | ❌ | ✅ |
| **Token scoping** | ❌ | ❌ | ✅ |
| **Setup complexity** | Low | Low | High |
| **Security level** | Low | Medium | High |
| **Production ready** | ❌ | ⚠️ Small teams | ✅ |

**Recommendation:**
- **Development**: Basic single-user
- **Small teams, no OIDC**: Basic multi-user (with HTTPS required)
- **Production**: OAuth mode with OIDC

### Phase 1: Add Management API to MCP Server

Create new REST API endpoints alongside existing MCP protocol endpoints:

```python
# nextcloud_mcp_server/api/management.py

from starlette.routing import Route
from starlette.responses import JSONResponse
from nextcloud_mcp_server.auth.management_auth import require_admin_or_self

@app.get("/api/v1/status")
async def get_server_status(request: Request) -> JSONResponse:
    """Server health and version info.

    Public endpoint, no authentication required.
    Returns basic server information for health checks.
    """
    from nextcloud_mcp_server import __version__
    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()

    return JSONResponse({
        "version": __version__,
        "auth_mode": "oauth" if settings.enable_oauth else "basic",
        "vector_sync_enabled": settings.vector_sync_enabled,
        "uptime_seconds": get_uptime(),
        "management_api_version": "v1",
    })

@app.get("/api/v1/users/{user_id}/session")
@require_admin_or_self
async def get_user_session(request: Request, user_id: str) -> JSONResponse:
    """Get user session details.

    Requires authentication. Users can view their own session,
    admins can view any session.

    Returns:
        - session_id: User identifier
        - background_access_granted: Whether refresh token exists
        - background_access_details: Flow type, scopes, provisioned_at
        - idp_profile: User profile from identity provider (if cached)
    """
    storage = request.app.state.storage

    # Get session metadata
    refresh_token_data = await storage.get_refresh_token(user_id)

    if not refresh_token_data:
        return JSONResponse({
            "session_id": user_id,
            "background_access_granted": False,
        })

    # Get cached user profile
    profile = await storage.get_user_profile(user_id)

    return JSONResponse({
        "session_id": user_id,
        "background_access_granted": True,
        "background_access_details": {
            "flow_type": refresh_token_data.get("flow_type", "unknown"),
            "provisioned_at": refresh_token_data.get("provisioned_at"),
            "scopes": refresh_token_data.get("scopes", "N/A"),
            "token_audience": refresh_token_data.get("token_audience", "unknown"),
        },
        "idp_profile": profile,
    })

@app.post("/api/v1/users/{user_id}/revoke")
@require_admin_or_self
async def revoke_user_access(request: Request, user_id: str) -> JSONResponse:
    """Revoke background access for user.

    Deletes the refresh token, preventing background operations
    from running on behalf of this user.

    Requires authentication. Users can revoke their own access,
    admins can revoke any user's access.
    """
    storage = request.app.state.storage
    await storage.delete_refresh_token(user_id)

    logger.info(f"Revoked background access for user: {user_id}")

    return JSONResponse({
        "success": True,
        "message": f"Background access revoked for user {user_id}",
    })

@app.get("/api/v1/vector-sync/status")
async def get_vector_sync_status(request: Request) -> JSONResponse:
    """Vector sync metrics.

    Public endpoint, no authentication required.
    Returns real-time indexing status and metrics.

    Requires: VECTOR_SYNC_ENABLED=true
    """
    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()
    if not settings.vector_sync_enabled:
        return JSONResponse(
            {"error": "Vector sync is disabled on this server"},
            status_code=404
        )

    # Get metrics from document manager
    from nextcloud_mcp_server.search.document_manager import get_indexing_metrics

    metrics = await get_indexing_metrics()

    return JSONResponse({
        "status": metrics.get("status", "unknown"),
        "indexed_documents": metrics.get("indexed_count", 0),
        "pending_documents": metrics.get("pending_count", 0),
        "last_sync_time": metrics.get("last_sync_time"),
        "documents_per_second": metrics.get("docs_per_second", 0),
        "errors_24h": metrics.get("error_count_24h", 0),
    })

@app.post("/api/v1/vector-viz/search")
@require_authenticated_user  # Requires valid OAuth token
async def vector_search(request: Request) -> JSONResponse:
    """Execute semantic search for visualization.

    AUTHENTICATION REQUIRED: User must be authenticated via OAuth token.
    Results are filtered to only include the authenticated user's documents.

    Request body:
        - query: Search query string
        - algorithm: "semantic", "bm25", or "hybrid" (default)
        - limit: Number of results (default: 10, max: 50)
        - include_pca: Whether to include PCA coordinates for 2D plot

    Returns:
        - results: Array of matching documents with scores (user's documents only)
        - pca_coordinates: 2D coordinates for visualization (if requested)
        - algorithm_used: Which search algorithm was used
        - total_documents: Total documents in corpus for this user
    """
    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()
    if not settings.vector_sync_enabled:
        return JSONResponse(
            {"error": "Vector sync is disabled on this server"},
            status_code=404
        )

    # Get authenticated user from OAuth token
    user_id, _ = await validate_token_and_get_user(request)

    data = await request.json()
    query = data.get("query", "")
    algorithm = data.get("algorithm", "hybrid")
    limit = min(int(data.get("limit", 10)), 50)
    include_pca = data.get("include_pca", True)

    if not query:
        return JSONResponse({"error": "Query is required"}, status_code=400)

    # Execute search filtered to user's documents
    from nextcloud_mcp_server.search.hybrid import search_documents

    results = await search_documents(
        query=query,
        filters={"user_id": user_id},  # CRITICAL: Filter by authenticated user
        algorithm=algorithm,
        limit=limit,
        include_pca=include_pca,
    )

    return JSONResponse(results)
```

**Authentication for Management API:**

The management API supports authentication methods corresponding to each deployment mode:

```python
# nextcloud_mcp_server/api/management.py

async def get_user_from_request(request: Request) -> tuple[str, dict]:
    """Authenticate request using deployment-appropriate method.

    Supports:
    - OAuth Bearer tokens (OAuth mode)
    - BasicAuth credentials (BasicAuth multi-user mode)
    - No authentication (BasicAuth single-user mode)

    Returns:
        Tuple of (user_id, auth_info)
    """
    from nextcloud_mcp_server.config import get_deployment_mode, DeploymentMode

    deployment_mode = get_deployment_mode()

    if deployment_mode == DeploymentMode.OAUTH:
        # OAuth mode: validate Bearer token
        return await validate_oauth_token(request)

    elif deployment_mode == DeploymentMode.BASIC_MULTIUSER:
        # BasicAuth multi-user: validate credentials
        return await validate_basic_auth(request)

    elif deployment_mode == DeploymentMode.BASIC:
        # Single-user: use env username
        settings = get_settings()
        return settings.nextcloud_username, {"type": "single_user"}

    else:
        raise ValueError(f"Unsupported deployment mode: {deployment_mode}")


async def validate_oauth_token(request: Request) -> tuple[str, dict]:
    """Validate OAuth bearer token (OAuth mode only).

    Uses the same UnifiedTokenVerifier as MCP client connections.
    Token audience must match the MCP server URL.
    """
    token = extract_bearer_token(request)
    if not token:
        raise ValueError("Missing Authorization header")

    # Get token verifier from app state
    token_verifier = request.app.state.oauth_context["token_verifier"]

    # Validate token - handles both JWT and opaque tokens
    access_token = await token_verifier.verify_token(token)
    if not access_token:
        raise ValueError("Token validation failed")

    user_id = access_token.resource
    if not user_id:
        raise ValueError("Token missing user identifier")

    return user_id, {
        "type": "oauth",
        "client_id": access_token.client_id,
        "scopes": access_token.scopes,
    }


async def validate_basic_auth(request: Request) -> tuple[str, str]:
    """Validate BasicAuth credentials (BasicAuth multi-user mode only).

    Validates credentials against Nextcloud to ensure they're valid.
    Does NOT store credentials - they're used only for this request.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise ValueError("Missing or invalid BasicAuth header")

    try:
        encoded = auth_header[6:]
        decoded = base64.b64decode(encoded).decode('utf-8')
        username, password = decoded.split(':', 1)
    except Exception:
        raise ValueError("Invalid BasicAuth encoding")

    # Validate credentials against Nextcloud
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.nextcloud_host}/ocs/v2.php/cloud/user",
            auth=(username, password),
            headers={"OCS-APIRequest": "true"},
        )

        if response.status_code != 200:
            raise ValueError("Invalid credentials")

    return username, {"type": "basic", "password": password}
```

**Key Design Points by Mode:**

**OAuth Mode:**
- ✅ Same token verifier as MCP clients (`UnifiedTokenVerifier`)
- ✅ Same audience requirement (token must be for MCP server URL)
- ✅ Self-service only (users access their own sessions)
- ✅ No shared secrets needed

**BasicAuth Multi-User:**
- ✅ Credentials validated against Nextcloud on each request
- ✅ Same credentials used by MCP clients
- ✅ Per-user identity preserved
- ⚠️ Requires HTTPS for security

**BasicAuth Single-User:**
- ✅ No authentication needed (single user assumed)
- ✅ Uses environment credentials
- ⚠️ Not suitable for multi-user deployments

**Add routes to app.py:**

```python
# In nextcloud_mcp_server/app.py

if settings.management_api_enabled:
    from nextcloud_mcp_server.api.management import (
        get_server_status,
        get_user_session,
        revoke_user_access,
        get_vector_sync_status,
        vector_search,
    )

    routes.extend([
        Route("/api/v1/status", get_server_status, methods=["GET"]),
        Route("/api/v1/users/{user_id}/session", get_user_session, methods=["GET"]),
        Route("/api/v1/users/{user_id}/revoke", revoke_user_access, methods=["POST"]),
        Route("/api/v1/vector-sync/status", get_vector_sync_status, methods=["GET"]),
        Route("/api/v1/vector-viz/search", vector_search, methods=["POST"]),
    ])

    logger.info("Management API enabled at /api/v1/*")
```

### Phase 2: Create Nextcloud PHP App

**Directory Structure:**

```
apps/nextcloud_mcp_admin/
├── appinfo/
│   ├── info.xml                    # App metadata, dependencies
│   ├── routes.php                  # Route definitions
│   └── app.php                     # App initialization
├── lib/
│   ├── AppInfo/
│   │   └── Application.php         # App container setup
│   ├── Controller/
│   │   ├── SettingsController.php  # Settings page controller
│   │   ├── VizController.php       # Vector viz controller
│   │   └── ApiController.php       # Proxy to management API
│   ├── Service/
│   │   └── McpServerClient.php     # HTTP client wrapper
│   └── Settings/
│       ├── AdminSettings.php       # Admin settings section
│       └── PersonalSettings.php    # Personal settings section
├── templates/
│   ├── settings/
│   │   ├── admin.php               # Admin panel template
│   │   └── personal.php            # User panel template
│   └── vector-viz.php              # Vector viz page
├── js/
│   ├── admin-settings.js           # Admin panel Vue.js app
│   ├── personal-settings.js        # Personal panel Vue.js app
│   └── vector-viz.js               # Vector viz (port from /app/static)
├── css/
│   └── styles.css                  # App styles
└── img/
    └── app.svg                     # App icon
```

**Example: McpServerClient.php**

```php
<?php
namespace OCA\MCPServerUI\Service;

use OCP\Http\Client\IClientService;
use OCP\IConfig;
use Psr\Log\LoggerInterface;

/**
 * HTTP client for MCP Server Management API.
 *
 * Uses OAuth Bearer tokens for authentication. The PHP app obtains tokens
 * through PKCE flow with the same audience as MCP clients.
 */
class McpServerClient {
    private $httpClient;
    private $config;
    private $logger;
    private $serverUrl;

    public function __construct(
        IClientService $clientService,
        IConfig $config,
        LoggerInterface $logger
    ) {
        $this->httpClient = $clientService->newClient();
        $this->config = $config;
        $this->logger = $logger;

        // Internal URL for server-to-server communication
        $this->serverUrl = $this->config->getSystemValue('mcp_server_url', 'http://localhost:8000');
    }

    /**
     * Get server status (version, auth mode, features)
     * Public endpoint - no authentication required.
     */
    public function getStatus(): array {
        try {
            $response = $this->httpClient->get($this->serverUrl . '/api/v1/status');
            return json_decode($response->getBody(), true);
        } catch (\Exception $e) {
            $this->logger->error('Failed to get MCP server status: ' . $e->getMessage());
            return ['error' => $e->getMessage()];
        }
    }

    /**
     * Get user session details.
     * Requires OAuth bearer token with matching user_id.
     *
     * @param string $userId The user ID to query
     * @param string $accessToken OAuth access token from PHP app's token storage
     */
    public function getUserSession(string $userId, string $accessToken): array {
        try {
            $response = $this->httpClient->get(
                $this->serverUrl . "/api/v1/users/$userId/session",
                [
                    'headers' => [
                        'Authorization' => "Bearer $accessToken"
                    ]
                ]
            );
            return json_decode($response->getBody(), true);
        } catch (\Exception $e) {
            $this->logger->error("Failed to get session for user $userId: " . $e->getMessage());
            return ['error' => $e->getMessage()];
        }
    }

    /**
     * Revoke user's background access.
     * Requires OAuth bearer token with matching user_id.
     *
     * @param string $userId The user ID whose access to revoke
     * @param string $accessToken OAuth access token from PHP app's token storage
     */
    public function revokeUserAccess(string $userId, string $accessToken): array {
        try {
            $response = $this->httpClient->post(
                $this->serverUrl . "/api/v1/users/$userId/revoke",
                [
                    'headers' => [
                        'Authorization' => "Bearer $accessToken"
                    ]
                ]
            );
            return json_decode($response->getBody(), true);
        } catch (\Exception $e) {
            $this->logger->error("Failed to revoke access for user $userId: " . $e->getMessage());
            return ['error' => $e->getMessage()];
        }
    }

    /**
     * Get vector sync status.
     * Public endpoint - no authentication required.
     */
    public function getVectorSyncStatus(): array {
        try {
            $response = $this->httpClient->get($this->serverUrl . '/api/v1/vector-sync/status');
            return json_decode($response->getBody(), true);
        } catch (\Exception $e) {
            $this->logger->error('Failed to get vector sync status: ' . $e->getMessage());
            return ['error' => $e->getMessage()];
        }
    }

    /**
     * Execute vector search for authenticated user.
     *
     * AUTHENTICATION: OAuth Bearer token required.
     * Results are filtered to the user associated with the token.
     *
     * @param string $query Search query
     * @param string $accessToken OAuth access token for the user
     * @param string $algorithm Search algorithm: 'semantic', 'bm25', or 'hybrid'
     * @param int $limit Maximum results
     * @param bool $includePca Include PCA visualization coordinates
     */
    public function search(
        string $query,
        string $accessToken,
        string $algorithm = 'hybrid',
        int $limit = 10,
        bool $includePca = false
    ): array {
        try {
            $response = $this->httpClient->post(
                $this->serverUrl . '/api/v1/search',
                [
                    'headers' => [
                        'Authorization' => "Bearer $accessToken",
                    ],
                    'json' => [
                        'query' => $query,
                        'algorithm' => $algorithm,
                        'limit' => $limit,
                        'include_pca' => $includePca,
                        'include_chunks' => true,
                    ]
                ]
            );
            return json_decode($response->getBody(), true);
        } catch (\Exception $e) {
            $this->logger->error("Failed to execute search: " . $e->getMessage());
            return ['error' => $e->getMessage()];
        }
    }

    /**
     * Get the public MCP server URL (for OAuth redirect_uri, display).
     */
    public function getPublicServerUrl(): string {
        return $this->config->getSystemValue('mcp_server_public_url', $this->serverUrl);
    }

    /**
     * Get the internal MCP server URL (for API calls).
     */
    public function getServerUrl(): string {
        return $this->serverUrl;
    }
}
```

**Example: PersonalSettings.php**

```php
<?php
namespace OCA\NextcloudMcpAdmin\Settings;

use OCA\NextcloudMcpAdmin\Service\McpServerClient;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\IUserSession;
use OCP\Settings\ISettings;

class Personal implements ISettings {
    private $client;
    private $userSession;

    public function __construct(
        McpServerClient $client,
        IUserSession $userSession
    ) {
        $this->client = $client;
        $this->userSession = $userSession;
    }

    /**
     * @return TemplateResponse
     */
    public function getForm() {
        $user = $this->userSession->getUser();
        if (!$user) {
            return new TemplateResponse('nextcloud_mcp_admin', 'error', [
                'message' => 'User not authenticated'
            ]);
        }

        $userId = $user->getUID();

        // Fetch data from MCP server
        $serverStatus = $this->client->getStatus();
        $userSession = $this->client->getUserSession($userId);

        $parameters = [
            'userId' => $userId,
            'serverStatus' => $serverStatus,
            'session' => $userSession,
            'vectorSyncEnabled' => $serverStatus['vector_sync_enabled'] ?? false,
            'backgroundAccessGranted' => $userSession['background_access_granted'] ?? false,
        ];

        return new TemplateResponse('nextcloud_mcp_admin', 'settings/personal', $parameters);
    }

    /**
     * @return string the section ID (e.g. 'additional')
     */
    public function getSection() {
        return 'additional';
    }

    /**
     * @return int priority (lower = higher up)
     */
    public function getPriority() {
        return 50;
    }
}
```

**Example: AdminSettings.php**

```php
<?php
namespace OCA\NextcloudMcpAdmin\Settings;

use OCA\NextcloudMcpAdmin\Service\McpServerClient;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\Settings\ISettings;

class Admin implements ISettings {
    private $client;

    public function __construct(McpServerClient $client) {
        $this->client = $client;
    }

    /**
     * @return TemplateResponse
     */
    public function getForm() {
        // Fetch data from MCP server
        $serverStatus = $this->client->getStatus();
        $vectorSyncStatus = $this->client->getVectorSyncStatus();

        $parameters = [
            'serverStatus' => $serverStatus,
            'vectorSyncStatus' => $vectorSyncStatus,
            'serverUrl' => $this->config->getSystemValue('mcp_server_url'),
        ];

        return new TemplateResponse('nextcloud_mcp_admin', 'settings/admin', $parameters);
    }

    /**
     * @return string the section ID
     */
    public function getSection() {
        return 'ai';  // Appears in "Artificial Intelligence" section
    }

    /**
     * @return int priority
     */
    public function getPriority() {
        return 10;
    }
}
```

**Example Template: personal.php**

```php
<?php
script('nextcloud_mcp_admin', 'personal-settings');
style('nextcloud_mcp_admin', 'styles');
?>

<div id="mcp-personal-settings" class="section">
    <h2><?php p($l->t('Nextcloud MCP Server')); ?></h2>

    <?php if (!empty($_['session']['error'])): ?>
        <div class="warning">
            <p><?php p($_['session']['error']); ?></p>
        </div>
    <?php else: ?>
        <div class="mcp-status-card">
            <h3><?php p($l->t('Session Information')); ?></h3>
            <table>
                <tr>
                    <td><strong><?php p($l->t('User ID')); ?></strong></td>
                    <td><code><?php p($_['userId']); ?></code></td>
                </tr>
                <tr>
                    <td><strong><?php p($l->t('Background Access')); ?></strong></td>
                    <td>
                        <?php if ($_['backgroundAccessGranted']): ?>
                            <span class="badge badge-success">✓ Granted</span>
                        <?php else: ?>
                            <span class="badge badge-neutral">Not Granted</span>
                        <?php endif; ?>
                    </td>
                </tr>
            </table>

            <?php if ($_['backgroundAccessGranted']): ?>
                <div class="mcp-background-details">
                    <h4><?php p($l->t('Background Access Details')); ?></h4>
                    <table>
                        <tr>
                            <td><strong><?php p($l->t('Flow Type')); ?></strong></td>
                            <td><?php p($_['session']['background_access_details']['flow_type']); ?></td>
                        </tr>
                        <tr>
                            <td><strong><?php p($l->t('Provisioned At')); ?></strong></td>
                            <td><?php p($_['session']['background_access_details']['provisioned_at']); ?></td>
                        </tr>
                        <tr>
                            <td><strong><?php p($l->t('Scopes')); ?></strong></td>
                            <td><code><?php p($_['session']['background_access_details']['scopes']); ?></code></td>
                        </tr>
                    </table>

                    <form method="post" action="<?php p($urlGenerator->linkToRoute('nextcloud_mcp_admin.api.revokeAccess')); ?>">
                        <input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
                        <button type="submit" class="button warning" onclick="return confirm('<?php p($l->t('Are you sure you want to revoke background access?')); ?>');">
                            <?php p($l->t('Revoke Background Access')); ?>
                        </button>
                    </form>
                </div>
            <?php endif; ?>
        </div>

        <?php if ($_['vectorSyncEnabled']): ?>
            <div class="mcp-vector-viz">
                <h3><?php p($l->t('Vector Visualization')); ?></h3>
                <p><?php p($l->t('Test semantic search and visualize results.')); ?></p>
                <a href="<?php p($urlGenerator->linkToRoute('nextcloud_mcp_admin.viz.index')); ?>" class="button primary">
                    <?php p($l->t('Open Vector Visualization')); ?>
                </a>
            </div>
        <?php endif; ?>
    <?php endif; ?>
</div>
```

### Phase 3: Configuration

**PHP App OAuth Client Registration:**

The PHP app needs an OAuth client registered with Nextcloud OIDC that:
1. Uses PKCE flow (public client, no client secret)
2. Has `resource_url` set to MCP server URL (for token audience)
3. Includes scopes for accessing MCP server features

```bash
# Register OAuth client for PHP app
php occ oidc:create \
    "MCP Server UI" \
    "http://localhost:8080/apps/mcpserverui/oauth/callback" \
    --client_id="nextcloudMcpServerUIPublicClient" \
    --type=public \
    --flow=code \
    --token_type=jwt \
    --resource_url="http://localhost:8001" \
    --allowed_scopes="openid profile email notes:read notes:write calendar:read ..."
```

**Nextcloud `config/config.php`:**

```php
<?php
$CONFIG = array(
    // ... existing configuration

    /**
     * MCP Server Internal URL
     *
     * URL for PHP app to reach MCP server management API.
     * In Docker, this is the internal container network URL.
     */
    'mcp_server_url' => 'http://mcp-oauth:8001',

    /**
     * MCP Server Public URL
     *
     * URL users/browsers see. Used for OAuth audience and display.
     * Must match the resource_url configured for the OAuth client.
     */
    'mcp_server_public_url' => 'http://localhost:8001',
);
```

**MCP Server `.env`:**

```bash
# === OAuth Configuration ===
NEXTCLOUD_HOST=http://app:80  # Internal URL for API calls
NEXTCLOUD_PUBLIC_ISSUER_URL=http://localhost:8080  # Public URL for token issuer

# OIDC Discovery
OIDC_DISCOVERY_URL=http://app:80/index.php/apps/oidc/.well-known/openid-configuration

# Token Audience
TOKEN_AUDIENCE=http://localhost:8001  # Must match PHP app's resource_url

# === Management API ===
# Automatically enabled in OAuth mode - uses same token verifier as MCP clients
# No separate API key needed

# === Disable Legacy Browser UI ===
ENABLE_BROWSER_UI=false

# === Vector Sync Configuration ===
VECTOR_SYNC_ENABLED=true
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
# ... etc
```

## Migration Path

### Release 0.53.0: Dual UI Support (Both Active)

**Changes:**
- ✅ Add management API to MCP server (`/api/v1/*`)
- ✅ Create Nextcloud PHP app (`apps/nextcloud_mcp_admin`)
- ✅ Keep existing `/app` endpoint active
- ✅ Add deprecation notice to `/app` UI

**User Impact:**
- Users can choose which UI to use
- Documentation explains migration path
- Both UIs fully functional

**Config:**
```bash
ENABLE_BROWSER_UI=true          # Default: keep legacy UI active
MANAGEMENT_API_ENABLED=true     # New: enable API for NC app
```

### Release 0.54.0: Transition (NC App Recommended)

**Changes:**
- ✅ NC PHP app is recommended approach in docs
- ✅ `/app` endpoint shows migration banner
- ⚠️ Default `ENABLE_BROWSER_UI=false` for new installations
- ✅ Existing deployments continue working (opt-in migration)

**User Impact:**
- New installations use NC app by default
- Existing installations see migration prompt
- Legacy `/app` still works if explicitly enabled

**Config:**
```bash
ENABLE_BROWSER_UI=false         # Changed default for new installs
MANAGEMENT_API_ENABLED=true     # Required for NC app
```

### Release 0.56.0: Deprecation (NC App Only)

**Changes:**
- ❌ Remove `/app` endpoint code entirely
- ❌ Remove browser OAuth routes
- ❌ Remove HTML templates, static files
- ✅ NC PHP app is the only UI

**User Impact:**
- Cleaner codebase
- Simpler deployment
- Native Nextcloud integration only

**Config:**
```bash
# ENABLE_BROWSER_UI removed (no longer exists)
MANAGEMENT_API_ENABLED=true     # Required
```

## Benefits

### ✅ MCP Protocol Integrity

**Preserves Full MCP Capabilities:**
- ✅ **Sampling support** - Server can request LLM completions from client (ADR-008)
- ✅ **Elicitation support** - Server can request user input via protocol
- ✅ **Bidirectional streaming** - Real-time progress updates, notifications
- ✅ **Persistent connections** - SSE/WebSocket support for MCP clients
- ✅ **External client integration** - Claude Desktop, custom clients work unchanged

**No ExApp Limitations:**
- The MCP server remains standalone with OAuth mode
- No AppAPI proxy blocking bidirectional communication
- All MCP protocol features work as designed
- Context Agent limitations (ADR-011) don't apply

### ✅ Native Nextcloud Integration

**Seamless User Experience:**
- Settings appear in standard Nextcloud settings interface
- Uses Nextcloud session (no separate login required)
- Follows Nextcloud design system and UX patterns
- Responsive design via Nextcloud's framework
- Accessibility built-in (NC accessibility standards)

**Familiar Navigation:**
- Personal settings: `/settings/user/additional`
- Admin settings: `/settings/admin/ai`
- Integrated with NC settings search
- Standard NC notifications and activity feed

### ✅ Simplified Deployment

**For Users:**
- One-click install from Nextcloud app store
- No separate URL to remember
- Single authentication (Nextcloud session)
- Managed through NC app management interface

**For Administrators:**
- MCP server remains standalone (Docker, systemd, etc.)
- Configure once in `config.php` (server URLs only)
- Register OAuth client once via `occ oidc:create`
- Update MCP server independently of Nextcloud
- Monitor via standard NC admin interface

### ✅ Better Security Model

**Unified OAuth Authentication:**
- Management API uses same OAuth tokens as MCP clients
- PHP app is another OAuth client with MCP server audience
- Single token verifier validates both MCP clients and PHP app
- No shared secrets or API keys required

**Clear Boundaries:**
- Read-only operations public (status, vector-sync)
- Write operations require valid OAuth token with matching user_id
- Users can only access/revoke their own sessions (self-service)

**Least Privilege:**
- PHP app has minimal permissions (UI rendering only)
- Tokens scoped to specific user and operations
- MCP server owns all business logic and state
- Audit trail via MCP server logs (user_id from token)

### ✅ Reduced Maintenance Burden

**Separation of Concerns:**
- MCP server: Python, FastMCP, protocol handling
- PHP app: Nextcloud integration, UI rendering
- No mixing of templating systems (Jinja2 removed)
- No duplicate authentication logic

**Simplified Codebase:**
- Remove `/app` routes, templates, static files (~2000 LOC)
- Remove browser OAuth flow (replaced by API authentication)
- Remove session management middleware
- Cleaner dependency tree (no HTMX, Alpine.js in Python)

### ✅ Multi-Tenant Support

**Flexible Architecture:**
- One NC PHP app → Multiple MCP servers (configure per-tenant)
- One MCP server → Multiple NC instances (shared management API)
- API key per tenant for isolation
- Independent scaling of UI and protocol layers

## Drawbacks and Mitigations

### Increased Deployment Complexity

**Drawback:** Users must deploy two components (MCP server + NC app) instead of one.

**Mitigation:**
- NC app is one-click install from app store
- MCP server deployment unchanged (Docker, systemd)
- Clear documentation with step-by-step guides
- Docker Compose example showing both components

### OAuth Client Registration

**Drawback:** Requires registering OAuth client for PHP app with correct audience.

**Mitigation:**
- One-time setup via `occ oidc:create` command
- Clear documentation with exact command to run
- Installation hook automates client registration in development
- Standard OAuth PKCE flow - well-understood security model

### Network Dependency

**Drawback:** NC app depends on MCP server being reachable via network.

**Mitigation:**
- Use internal URLs (localhost, Docker networks)
- Graceful degradation if server unavailable
- Clear error messages with troubleshooting steps
- Health check endpoint (`/api/v1/status`)

### Code Duplication (UI Logic)

**Drawback:** UI rendering logic exists in both Python templates and PHP templates.

**Mitigation:**
- Phase 1: Port existing Jinja2 templates to PHP
- Phase 2: Remove Python templates when NC app is stable
- Single source of truth: MCP server owns all business logic
- PHP app is stateless view layer only

## Alternatives Considered

### Alternative 1: Keep Current `/app` Endpoint

**Description:** Continue with standalone browser UI at `/app`.

**Pros:**
- No changes required
- Works today
- Simple deployment (one component)

**Cons:**
- ❌ Separate authentication system
- ❌ No Nextcloud integration
- ❌ Maintains Python templating burden
- ❌ Users must remember separate URL

**Rejected Because:** Users want native NC integration, and maintaining dual UIs is high maintenance burden.

### Alternative 2: Run MCP Server as ExApp

**Description:** Deploy MCP server as Nextcloud ExApp (investigated in ADR-011).

**Pros:**
- ✅ Deep Nextcloud integration
- ✅ Native UI components
- ✅ One-click installation

**Cons:**
- ❌ **No MCP sampling** - AppAPI proxy blocks bidirectional protocol
- ❌ **No real-time progress** - Request/response model only
- ❌ **Buffered streaming** - Not incremental
- ❌ **Fundamental protocol incompatibility**

**Rejected Because:** ExApp architecture cannot support MCP protocol requirements. See ADR-011 for comprehensive analysis.

### Alternative 3: Embed MCP Server in Nextcloud

**Description:** Port entire MCP server to PHP, run inside Nextcloud process.

**Pros:**
- ✅ Maximum integration
- ✅ Single deployment artifact
- ✅ No network boundary

**Cons:**
- ❌ **Massive rewrite** - 20,000+ LOC Python → PHP
- ❌ **No async support** - PHP lacks Python's async/await model
- ❌ **Performance issues** - Background workers in request context
- ❌ **Dependency hell** - Qdrant, embeddings, vector ops in PHP
- ❌ **Loss of ecosystem** - FastMCP, httpx, pydantic, anyio

**Rejected Because:** Impractical to port and would lose Python ecosystem benefits.

### Alternative 4: Iframe Embedding

**Description:** Keep `/app` endpoint, embed in NC using iframe.

**Pros:**
- ✅ Minimal changes
- ✅ Works quickly

**Cons:**
- ❌ **Poor UX** - Iframe scroll issues, double nav bars
- ❌ **Security issues** - CORS, CSP complexity
- ❌ **Mobile unfriendly** - Terrible on small screens
- ❌ **Still separate auth** - Doesn't solve login problem

**Rejected Because:** Iframe embedding provides poor user experience and doesn't solve core problems.

### Alternative 5: MCP Protocol Proxy in PHP App

**Description:** Implement MCP protocol (Streamable HTTP/SSE) directly in PHP app to proxy requests from external clients to the MCP container.

**Proposed Architecture:**
```
External MCP Client → Astrolabe PHP App (SSE proxy) → MCP Container
```

**Pros:**
- ✅ Single URL for all access
- ✅ Nextcloud hostname for MCP endpoints
- ✅ Centralized routing

**Cons:**
- ❌ **PHP SSE limitations** - Request-response model fights streaming
  - Long-lived connections cause PHP-FPM timeout issues
  - Memory leaks from buffering
  - Requires custom FPM pool configuration
- ❌ **Web server buffering** - Nginx/Apache buffer by default
  - Defeats SSE real-time nature
  - Requires server-specific config (`X-Accel-Buffering: no`)
- ❌ **Complex implementation** - ~1000+ LOC SSE proxy logic
  - Session management in PHP (MCP-Session-Id headers)
  - SSE event stream parsing and forwarding
  - Resumability handling (Last-Event-ID)
  - Connection keep-alive management
- ❌ **Duplicated logic** - Reimplements what container already does perfectly
- ❌ **No additional value** - External clients still need same auth (OAuth/BasicAuth)
  - PHP layer adds latency, not features
  - Authentication still delegated to MCP server
- ❌ **Maintenance burden** - Fighting PHP's architecture for streaming

**Better Alternative:** Direct reverse proxy (nginx/Apache config):
```nginx
# 10 lines of nginx config vs 1000+ lines of PHP
location /mcp/ {
    proxy_pass http://mcp-container:8001/;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

**Rejected Because:**
- PHP is fundamentally ill-suited for SSE proxying (request-response vs streaming)
- Reverse proxy at web server layer is simpler, more performant, and standard practice
- No value added by PHP layer - authentication handled by container regardless
- Rest API for Astrolabe UI is sufficient for internal NC integration

**Note:** This alternative would have made sense if it enabled new use cases. However:
1. **External MCP clients**: Work fine connecting directly to container (via reverse proxy)
2. **Astrolabe UI**: Doesn't need MCP protocol - REST API sufficient
3. **Authentication**: Solved by BasicAuth multi-user mode (pass-through) for non-OIDC deployments

The REST API + BasicAuth multi-user architecture achieves the same goals (multi-user access without OIDC) without the complexity of SSE proxying.

### Alternative 6: Nextcloud Frontend App (Vue.js SPA)

**Description:** Build NC frontend app (JavaScript only), talk to management API.

**Pros:**
- ✅ Modern frontend framework
- ✅ Rich interactivity
- ✅ API-driven architecture

**Cons:**
- ❌ **More complex** - Requires JavaScript build pipeline
- ❌ **Deployment overhead** - Webpack, npm, CI/CD for frontend
- ❌ **Not standard NC pattern** - Most NC apps use server-side templates
- ❌ **Accessibility harder** - Must implement manually

**Rejected Because:** Traditional NC PHP app provides better integration and simpler deployment.

## Implementation Plan

### Phase 1: Management API (Week 1-2)

**Goal:** Add REST API to MCP server without breaking existing functionality.

**Tasks:**
1. Create `nextcloud_mcp_server/api/management.py`
2. Implement core endpoints:
   - `GET /api/v1/status`
   - `GET /api/v1/users/{id}/session`
   - `POST /api/v1/users/{id}/revoke`
   - `GET /api/v1/vector-sync/status`
   - `POST /api/v1/vector-viz/search`
3. Implement `management_auth.py` with `require_admin_or_self`
4. Add API key authentication support
5. Add routes to `app.py` (behind `MANAGEMENT_API_ENABLED` flag)
6. Write integration tests for all endpoints
7. Update `.env.example` with new config options

**Success Criteria:**
- All management endpoints return correct data
- API key authentication works
- `/app` endpoint unchanged and working
- Tests pass

### Phase 2: Nextcloud PHP App Scaffolding (Week 3-4)

**Goal:** Create basic NC app structure that can display data.

**Tasks:**
1. Create app directory structure
2. Write `appinfo/info.xml` with metadata
3. Implement `McpServerClient.php` service
4. Create `PersonalSettings.php` and `AdminSettings.php`
5. Port basic templates from Jinja2 to PHP
6. Add simple CSS styling
7. Test installation in development NC instance

**Success Criteria:**
- App installs without errors
- Personal settings panel appears
- Admin settings panel appears
- Can connect to MCP server API
- Displays basic user info

### Phase 3: Feature Parity (Week 5-7)

**Goal:** Port all `/app` features to NC PHP app.

**Tasks:**
1. **Vector Sync Tab:**
   - Port auto-refresh HTMX functionality
   - Display real-time metrics
   - Sync status indicators
2. **Vector Visualization Tab:**
   - Port Plotly.js integration
   - Port `vector-viz.js` from `/app/static`
   - Interactive search interface
   - 2D PCA visualization
3. **Webhook Management:**
   - Admin-only tab
   - Enable/disable presets
   - Status display
4. **Session Management:**
   - Display session details
   - Revoke access button
   - OAuth flow integration
5. Polish UI/UX:
   - Responsive design
   - Loading states
   - Error handling

**Success Criteria:**
- All `/app` features available in NC app
- Feature parity achieved
- UI polished and responsive

### Phase 4: Documentation and Migration (Week 8)

**Goal:** Prepare for release with clear migration path.

**Tasks:**
1. Write this ADR (ADR-018)
2. Update `docs/installation.md` with NC app instructions
3. Update `docs/configuration.md` with management API settings
4. Create migration guide for existing `/app` users
5. Add deprecation notice to `/app` endpoint
6. Create demo video showing NC app features
7. Update README with new architecture diagram

**Success Criteria:**
- Documentation complete and accurate
- Migration path clear
- Deprecation notices visible

### Phase 5: Release 0.53.0 (Week 9-10)

**Goal:** Ship dual UI support (both `/app` and NC app work).

**Tasks:**
1. Final testing of management API
2. Final testing of NC app
3. Release notes
4. Publish NC app to app store
5. Tag release v0.53.0
6. Monitor for issues

**Success Criteria:**
- Both UIs functional
- No regressions
- Users can migrate at own pace

### Phase 6: Deprecation (Release 0.54.0, ~3 months later)

**Goal:** Make NC app the default for new installations.

**Tasks:**
1. Change default `ENABLE_BROWSER_UI=false`
2. Add migration banner to `/app` endpoint
3. Update all documentation to recommend NC app
4. Announce deprecation timeline

**Success Criteria:**
- New users default to NC app
- Existing users notified of migration

### Phase 7: Removal (Release 0.56.0, ~6 months later)

**Goal:** Remove legacy `/app` code entirely.

**Tasks:**
1. Remove `/app` routes from `app.py`
2. Remove `auth/browser_oauth_routes.py`
3. Remove templates (`auth/templates/`)
4. Remove static files (`auth/static/`)
5. Remove session authentication middleware
6. Update tests to remove `/app` references
7. Simplify dependencies (remove Jinja2 if only for `/app`)

**Success Criteria:**
- Cleaner codebase (~2000 LOC removed)
- NC app is only UI
- All tests pass

## Success Metrics

**Technical Metrics:**
- Management API response time <100ms (p95)
- Zero MCP protocol regressions
- 95%+ feature parity with `/app` endpoint
- <5% error rate on API calls

**User Experience Metrics:**
- 90%+ of users migrate to NC app within 6 months
- <10 support requests related to NC app setup
- Positive feedback on UX integration
- Reduced time-to-first-use for new users

**Maintenance Metrics:**
- 30%+ reduction in UI-related code
- Fewer dependency updates (no browser JS in Python)
- Cleaner separation of concerns (API vs UI)
- Faster feature development (standard NC patterns)

## Optional Enhancement: Unified Search Provider

### Background

Nextcloud's **Unified Search** (introduced in Nextcloud 20) provides a pluggable architecture where apps register search providers. This allows the MCP server's semantic search capabilities to appear in Nextcloud's global search bar, providing users with AI-powered search results alongside traditional file/app searches.

**References:**
- [Nextcloud Developer Manual: Search](https://docs.nextcloud.com/server/latest/developer_manual/digging_deeper/search.html)
- Nextcloud 28+ supports `IFilteringProvider` for advanced filtering
- Nextcloud 32+ supports `IExternalProvider` for privacy-aware external searches

### Architecture

The PHP app can register a search provider that delegates semantic search to the MCP server's management API:

```
User types in NC search bar → Unified Search → PHP App Search Provider
                                                        │
                                                        │ (user_id from NC session)
                                                        ▼
                                              POST /api/v1/search
                                              Authorization: Bearer <token>
                                              Body: { query, user_id }
                                                        │
                                                        ▼
                                              MCP Server validates token,
                                              filters results by user_id
                                                        │
                                                        ▼
                                              Only user's documents returned
```

### User-Scoped Search (Security Model)

**Critical Requirement:** Search results must be filtered to only include documents the searching user has permission to access. The vector database contains documents from potentially multiple users, and returning unfiltered results would be a serious security vulnerability.

#### Permission Model

**Phase 1: Owner-Only Filtering (Initial Implementation)**

The simplest and most secure approach filters results to documents owned by the searching user:

| Document Type | Filter Logic |
|---------------|--------------|
| Notes | `metadata.user_id == searching_user` |
| Files | `metadata.user_id == searching_user` |
| Deck Cards | `metadata.user_id == searching_user` (card creator) |
| Calendar Events | `metadata.user_id == searching_user` |

**Limitation:** Users cannot find content shared with them. This is acceptable for initial implementation because:
- It's secure by default (no accidental data leakage)
- Covers the primary use case (searching your own content)
- Shared content support can be added incrementally

**Phase 2: Shared Content Support (Future Enhancement)**

To support searching shared content, additional metadata must be indexed:

```python
# Extended metadata for sharing support
document_metadata = {
    "user_id": "alice",           # Owner
    "shared_with_users": ["bob", "charlie"],  # Direct shares
    "shared_with_groups": ["developers"],      # Group shares
    "is_public": False,           # Public link exists
    "share_permissions": "read",  # read, write, reshare
}
```

Search filter becomes:
```python
filter = (
    (metadata.user_id == searching_user) |
    (searching_user in metadata.shared_with_users) |
    (any(g in user_groups for g in metadata.shared_with_groups)) |
    (metadata.is_public == True)
)
```

**Challenges for Phase 2:**
- Share metadata becomes stale when shares change
- Requires webhook integration with NC sharing events
- Group membership lookups add latency
- Complex ACL models (federated shares, circles) are hard to index

#### Authentication Flow

The search endpoint uses the same OAuth authentication as all other protected endpoints:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  PHP App (Search Provider)                                              │
│                                                                         │
│  1. Receives IUser $user from Nextcloud session                         │
│  2. Gets OAuth token for user (via NC OIDC, cached)                     │
│  3. Calls MCP server with Bearer token                                  │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  MCP Server /api/v1/search                                              │
│                                                                         │
│  1. Validates OAuth Bearer token (same as other endpoints)              │
│  2. Extracts user_id from token (sub claim)                             │
│  3. Queries vector DB with filter: user_id == token.sub                 │
│  4. Returns only documents owned by authenticated user                  │
└─────────────────────────────────────────────────────────────────────────┘
```

This uses the existing OAuth infrastructure - no additional authentication mechanism needed. The PHP app obtains tokens through NC's OIDC provider, same as MCP clients.

### Implementation

**Search Provider Class:**

```php
<?php

declare(strict_types=1);

namespace OCA\MCPServerUI\Search;

use OCA\MCPServerUI\AppInfo\Application;
use OCA\MCPServerUI\Service\McpServerClient;
use OCA\MCPServerUI\Service\TokenService;
use OCP\IL10N;
use OCP\IURLGenerator;
use OCP\IUser;
use OCP\Search\IExternalProvider;
use OCP\Search\IProvider;
use OCP\Search\ISearchQuery;
use OCP\Search\SearchResult;
use OCP\Search\SearchResultEntry;

/**
 * Unified Search provider for MCP Server semantic search.
 *
 * Delegates search queries to the MCP server's vector search API,
 * returning semantically relevant results from indexed Nextcloud content
 * (notes, files, calendar, deck cards).
 *
 * Implements IExternalProvider (NC 32+) because search is performed
 * by an external service (MCP server with vector database).
 */
class SemanticSearchProvider implements IProvider, IExternalProvider {

    public function __construct(
        private McpServerClient $client,
        private TokenService $tokenService,  // Manages OAuth tokens for users
        private IL10N $l10n,
        private IURLGenerator $urlGenerator,
    ) {
    }

    /**
     * Unique identifier for this search provider.
     * Prefixed with app ID to avoid conflicts.
     */
    public function getId(): string {
        return Application::APP_ID . '_semantic';
    }

    /**
     * Display name shown in search results grouping.
     */
    public function getName(): string {
        return $this->l10n->t('AI Search');
    }

    /**
     * Order in search results. Lower = higher priority.
     * Use negative value when user is in our app's context.
     */
    public function getOrder(string $route, array $routeParameters): int {
        if (str_contains($route, Application::APP_ID)) {
            return -1;  // Prioritize when in MCP Server UI
        }
        return 40;  // Above most apps, below files/mail
    }

    /**
     * Indicates this is an external search provider (NC 32+).
     * External providers are disabled by default in Unified Search UI
     * for privacy reasons - user must opt-in via toggle.
     */
    public function isExternalProvider(): bool {
        return true;
    }

    /**
     * Execute semantic search via MCP server.
     *
     * SECURITY: Results are filtered server-side to only include documents
     * owned by the searching user. User identity comes from OAuth token.
     */
    public function search(IUser $user, ISearchQuery $query): SearchResult {
        $term = $query->getTerm();
        $limit = $query->getLimit();
        $cursor = $query->getCursor();

        // Skip empty queries
        if (empty(trim($term))) {
            return SearchResult::complete($this->getName(), []);
        }

        // Get OAuth token for user (cached, refreshed automatically)
        try {
            $accessToken = $this->tokenService->getAccessToken($user->getUID());
        } catch (\Exception $e) {
            // User hasn't authorized the app yet - return empty results
            return SearchResult::complete($this->getName(), []);
        }

        // Check if MCP server is available and vector sync enabled
        $status = $this->client->getStatus();
        if (!empty($status['error']) || !($status['vector_sync_enabled'] ?? false)) {
            return SearchResult::complete($this->getName(), []);
        }

        // Execute semantic search with OAuth token
        // Server extracts user_id from token - results filtered to that user's documents
        $offset = $cursor ? (int)$cursor : 0;
        $results = $this->client->search(
            query: $term,
            accessToken: $accessToken,  // User identity from OAuth token
            algorithm: 'hybrid',
            limit: $limit,
        );

        if (!empty($results['error'])) {
            return SearchResult::complete($this->getName(), []);
        }

        // Transform results to SearchResultEntry objects
        $entries = [];
        foreach ($results['results'] ?? [] as $result) {
            $entries[] = $this->transformResult($result);
        }

        // Return paginated if more results might exist
        $totalFound = $results['total_found'] ?? count($entries);
        if (count($entries) >= $limit && $totalFound > $offset + $limit) {
            return SearchResult::paginated(
                $this->getName(),
                $entries,
                $offset + $limit
            );
        }

        return SearchResult::complete($this->getName(), $entries);
    }

    /**
     * Transform MCP search result to Nextcloud SearchResultEntry.
     */
    private function transformResult(array $result): SearchResultEntry {
        $docType = $result['doc_type'] ?? 'unknown';
        $title = $result['title'] ?? 'Untitled';
        $excerpt = $result['excerpt'] ?? '';
        $score = $result['score'] ?? 0;

        // Build resource URL based on document type
        $resourceUrl = $this->buildResourceUrl($result);

        // Build thumbnail URL based on document type
        $thumbnailUrl = $this->buildThumbnailUrl($docType);

        // Subline shows document type and relevance score
        $subline = sprintf(
            '%s • %.0f%% relevant',
            $this->getDocTypeLabel($docType),
            $score * 100
        );

        $entry = new SearchResultEntry(
            $thumbnailUrl,
            $title,
            $subline,
            $resourceUrl,
            '', // icon class (empty, using thumbnail)
            false // not rounded
        );

        // Add optional attributes for mobile clients
        $entry->addAttribute('type', $docType);
        $entry->addAttribute('score', (string)$score);

        if (isset($result['id'])) {
            $entry->addAttribute('docId', (string)$result['id']);
        }

        return $entry;
    }

    /**
     * Build URL to navigate to the original document.
     */
    private function buildResourceUrl(array $result): string {
        $docType = $result['doc_type'] ?? 'unknown';
        $id = $result['id'] ?? null;
        $path = $result['path'] ?? null;

        return match ($docType) {
            'note' => $id
                ? $this->urlGenerator->linkToRoute('notes.page.index') . '#/note/' . $id
                : $this->urlGenerator->linkToRoute('notes.page.index'),

            'file' => $path
                ? $this->urlGenerator->linkToRoute('files.view.index', [
                    'dir' => dirname($path),
                    'scrollto' => basename($path),
                ])
                : $this->urlGenerator->linkToRoute('files.view.index'),

            'deck_card' => isset($result['board_id'], $result['card_id'])
                ? $this->urlGenerator->linkToRoute('deck.page.index') .
                  "#!/board/{$result['board_id']}/card/{$result['card_id']}"
                : $this->urlGenerator->linkToRoute('deck.page.index'),

            'calendar_event' => $this->urlGenerator->linkToRoute('calendar.view.index'),

            default => $this->urlGenerator->linkToRoute(Application::APP_ID . '.page.index'),
        };
    }

    /**
     * Get thumbnail URL for document type.
     */
    private function buildThumbnailUrl(string $docType): string {
        return match ($docType) {
            'note' => $this->urlGenerator->imagePath('notes', 'app.svg'),
            'file' => $this->urlGenerator->imagePath('files', 'app.svg'),
            'deck_card' => $this->urlGenerator->imagePath('deck', 'app.svg'),
            'calendar_event' => $this->urlGenerator->imagePath('calendar', 'app.svg'),
            default => $this->urlGenerator->imagePath(Application::APP_ID, 'app.svg'),
        };
    }

    /**
     * Get human-readable label for document type.
     */
    private function getDocTypeLabel(string $docType): string {
        return match ($docType) {
            'note' => $this->l10n->t('Note'),
            'file' => $this->l10n->t('File'),
            'deck_card' => $this->l10n->t('Deck Card'),
            'calendar_event' => $this->l10n->t('Calendar'),
            'news_item' => $this->l10n->t('News'),
            default => $this->l10n->t('Document'),
        };
    }
}
```

**Provider Registration in Application.php:**

```php
<?php

declare(strict_types=1);

namespace OCA\MCPServerUI\AppInfo;

use OCA\MCPServerUI\Search\SemanticSearchProvider;
use OCP\AppFramework\App;
use OCP\AppFramework\Bootstrap\IBootContext;
use OCP\AppFramework\Bootstrap\IBootstrap;
use OCP\AppFramework\Bootstrap\IRegistrationContext;

class Application extends App implements IBootstrap {

    public const APP_ID = 'mcpserverui';

    public function __construct(array $urlParams = []) {
        parent::__construct(self::APP_ID, $urlParams);
    }

    public function register(IRegistrationContext $context): void {
        // Register unified search provider
        $context->registerSearchProvider(SemanticSearchProvider::class);

        // ... other registrations
    }

    public function boot(IBootContext $context): void {
        // ... boot logic
    }
}
```

**TokenService - Managing OAuth Tokens:**

The `TokenService` handles obtaining and caching OAuth tokens for users:

```php
<?php

declare(strict_types=1);

namespace OCA\MCPServerUI\Service;

use OCP\IConfig;
use OCP\IUserSession;
use Psr\Log\LoggerInterface;

/**
 * Manages OAuth access tokens for MCP server communication.
 *
 * Tokens are obtained through NC's OIDC provider and cached per-user.
 * The service handles token refresh automatically when tokens expire.
 */
class TokenService {

    public function __construct(
        private IConfig $config,
        private IUserSession $userSession,
        private LoggerInterface $logger,
    ) {
    }

    /**
     * Get a valid access token for the user.
     *
     * Returns cached token if still valid, otherwise refreshes or
     * throws exception if user hasn't authorized the app.
     *
     * @throws \Exception If user hasn't authorized the app
     */
    public function getAccessToken(string $userId): string {
        // Check for cached token
        $tokenData = $this->getCachedToken($userId);

        if ($tokenData && !$this->isExpired($tokenData)) {
            return $tokenData['access_token'];
        }

        // Try to refresh if we have a refresh token
        if ($tokenData && isset($tokenData['refresh_token'])) {
            try {
                return $this->refreshToken($userId, $tokenData['refresh_token']);
            } catch (\Exception $e) {
                $this->logger->warning("Token refresh failed for user $userId: " . $e->getMessage());
            }
        }

        // No valid token - user needs to authorize
        throw new \Exception("User $userId has not authorized the app");
    }

    /**
     * Store token data after user authorization.
     */
    public function storeToken(string $userId, array $tokenData): void {
        $tokenData['stored_at'] = time();
        $this->config->setUserValue(
            $userId,
            Application::APP_ID,
            'oauth_token',
            json_encode($tokenData)
        );
    }

    /**
     * Check if user has authorized the app.
     */
    public function hasAuthorized(string $userId): bool {
        try {
            $this->getAccessToken($userId);
            return true;
        } catch (\Exception $e) {
            return false;
        }
    }

    private function getCachedToken(string $userId): ?array {
        $data = $this->config->getUserValue($userId, Application::APP_ID, 'oauth_token', '');
        return $data ? json_decode($data, true) : null;
    }

    private function isExpired(array $tokenData): bool {
        if (!isset($tokenData['expires_in'], $tokenData['stored_at'])) {
            return true;
        }
        // Add 60s buffer before expiry
        return time() > ($tokenData['stored_at'] + $tokenData['expires_in'] - 60);
    }

    private function refreshToken(string $userId, string $refreshToken): string {
        // Call NC OIDC token endpoint with refresh_token grant
        // ... implementation details ...
    }
}
```

### Privacy Considerations

The search provider implements `IExternalProvider` (Nextcloud 32+) because:

1. **External Processing**: Search queries are sent to the MCP server, which may run on a different host
2. **Vector Database**: Embeddings are stored in an external Qdrant instance
3. **User Consent**: NC's Unified Search UI shows external providers with a toggle, requiring user opt-in

For Nextcloud versions before 32, the provider should check user preferences before executing searches:

```php
public function search(IUser $user, ISearchQuery $query): SearchResult {
    // Check user preference for external search
    $enabled = $this->config->getUserValue(
        $user->getUID(),
        Application::APP_ID,
        'enable_unified_search',
        'false'
    );

    if ($enabled !== 'true') {
        return SearchResult::complete($this->getName(), []);
    }

    // ... proceed with search
}
```

### Advanced Filtering (Nextcloud 28+)

For Nextcloud 28+, implement `IFilteringProvider` to support advanced search filters:

```php
<?php

declare(strict_types=1);

namespace OCA\MCPServerUI\Search;

use OCP\Search\FilterDefinition;
use OCP\Search\IFilteringProvider;

class SemanticSearchProvider implements IFilteringProvider, IExternalProvider {

    // ... existing methods ...

    /**
     * Declare supported standard filters.
     */
    public function getSupportedFilters(): array {
        return [
            'term',           // Search query
            'since',          // Date filter (document modified after)
            'until',          // Date filter (document modified before)
        ];
    }

    /**
     * Declare custom filters specific to this provider.
     */
    public function getCustomFilters(): array {
        return [
            new FilterDefinition('doc_type', FilterDefinition::TYPE_STRING),
            new FilterDefinition('min_score', FilterDefinition::TYPE_FLOAT),
        ];
    }

    /**
     * Alternate IDs that trigger this provider.
     */
    public function getAlternateIds(): array {
        return ['semantic', 'ai'];
    }

    public function search(IUser $user, ISearchQuery $query): SearchResult {
        // Retrieve filters
        $term = $query->getTerm();
        $since = $query->getFilter('since')?->get();
        $docType = $query->getFilter('doc_type')?->get();
        $minScore = $query->getFilter('min_score')?->get() ?? 0.0;

        // Pass filters to MCP server
        $results = $this->client->searchWithFilters(
            query: $term,
            doc_types: $docType ? [$docType] : null,
            score_threshold: $minScore,
            modified_after: $since?->format('c'),
        );

        // ... transform and return results
    }
}
```

### MCP Server API Extension

The search endpoint uses OAuth authentication (same as other protected endpoints) and includes visualization support:

```python
from starlette.responses import JSONResponse
from starlette.requests import Request

from nextcloud_mcp_server.api.management_auth import require_authenticated_user


@app.post("/api/v1/search")
@require_authenticated_user  # Same OAuth validation as other endpoints
async def unified_search(request: Request) -> JSONResponse:
    """Search endpoint for Nextcloud Unified Search provider and vector visualization.

    AUTHENTICATION: OAuth Bearer token required (same as other protected endpoints).
    User identity extracted from token - results filtered to that user's documents.

    Parameters:
    - query: Search query string (required)
    - algorithm: "semantic", "bm25", or "hybrid" (default: "hybrid")
    - doc_types: Filter by document type (optional)
    - score_threshold: Minimum relevance score (optional, default: 0.0)
    - limit: Max results (default: 20, max: 100)
    - offset: Pagination offset (default: 0)
    - include_pca: Include 2D PCA coordinates for visualization (default: false)
    - include_chunks: Include matched text chunks/snippets (default: true)

    Returns:
    - results: Array of matching documents (user's documents only)
      - Each result includes: id, title, doc_type, score, excerpt
      - If include_chunks: matched_chunks with highlighted text
      - If include_pca: pca_x, pca_y coordinates
    - total_found: Total matching documents for pagination
    - pca_data: Global PCA data for visualization (if include_pca)
      - query_point: [x, y] coordinates of the query
      - corpus_sample: Sample of corpus points for context

    Security:
    - User ID extracted from validated OAuth token
    - Results ALWAYS filtered by user_id from token
    - No cross-user data leakage possible
    """
    from nextcloud_mcp_server.config import get_settings

    settings = get_settings()
    if not settings.vector_sync_enabled:
        return JSONResponse(
            {"error": "Vector sync is disabled"},
            status_code=404
        )

    # Extract user_id from validated OAuth token
    user_id, _ = await validate_token_and_get_user(request)

    data = await request.json()
    query = data.get("query", "")

    if not query:
        return JSONResponse({"results": [], "total_found": 0})

    # Execute search with mandatory user filter
    from nextcloud_mcp_server.search.hybrid import search_documents

    results = await search_documents(
        query=query,
        # CRITICAL: Filter by user_id from OAuth token
        filters={"user_id": user_id},
        algorithm=data.get("algorithm", "hybrid"),
        doc_types=data.get("doc_types"),
        score_threshold=data.get("score_threshold", 0.0),
        limit=min(data.get("limit", 20), 100),
        offset=data.get("offset", 0),
        include_pca=data.get("include_pca", False),
        include_chunks=data.get("include_chunks", True),
    )

    return JSONResponse({
        "results": results["results"],
        "total_found": results.get("total_found", len(results["results"])),
        "algorithm_used": results.get("algorithm_used", "hybrid"),
        # Visualization data (only if requested)
        "pca_data": results.get("pca_data") if data.get("include_pca") else None,
    })
```

**Updated McpServerClient.php:**

```php
/**
 * Execute semantic search for authenticated user.
 *
 * Results are automatically filtered to the user associated with the OAuth token.
 *
 * @param string $query Search query
 * @param string $accessToken OAuth access token for the user
 * @param string $algorithm Search algorithm: 'semantic', 'bm25', or 'hybrid'
 * @param int $limit Maximum results to return
 * @param int $offset Pagination offset
 * @param bool $includePca Include PCA coordinates for visualization
 * @param bool $includeChunks Include matched text chunks/snippets
 */
public function search(
    string $query,
    string $accessToken,
    string $algorithm = 'hybrid',
    int $limit = 20,
    int $offset = 0,
    bool $includePca = false,
    bool $includeChunks = true
): array {
    try {
        $response = $this->httpClient->post(
            $this->serverUrl . '/api/v1/search',
            [
                'headers' => [
                    'Authorization' => "Bearer $accessToken",
                    'Content-Type' => 'application/json',
                ],
                'json' => [
                    'query' => $query,
                    'algorithm' => $algorithm,
                    'limit' => $limit,
                    'offset' => $offset,
                    'include_pca' => $includePca,
                    'include_chunks' => $includeChunks,
                ]
            ]
        );
        return json_decode($response->getBody(), true);
    } catch (\Exception $e) {
        $this->logger->error("Search failed: " . $e->getMessage());
        return ['error' => $e->getMessage()];
    }
}
```

### Benefits

1. **Native Integration**: Semantic search appears in Nextcloud's global search bar
2. **Unified Experience**: Users search once, get results from all sources
3. **Privacy-Aware**: External provider status informs users about data flow
4. **Mobile Support**: Results include attributes for mobile clients
5. **Advanced Filtering**: Date ranges, document types, relevance thresholds

### Implementation Timeline

This is an **optional enhancement** that can be added after the core PHP app is stable:

- **Phase 5+**: Add basic search provider (implements `IProvider`)
- **Phase 6+**: Add advanced filtering (implements `IFilteringProvider`)
- **Phase 7+**: Add external provider marking (implements `IExternalProvider` for NC 32+)

### Testing

```php
<?php

namespace OCA\MCPServerUI\Tests\Search;

use OCA\MCPServerUI\Search\SemanticSearchProvider;
use OCA\MCPServerUI\Service\McpServerClient;
use OCP\IL10N;
use OCP\IURLGenerator;
use OCP\IUser;
use OCP\Search\ISearchQuery;
use PHPUnit\Framework\TestCase;

class SemanticSearchProviderTest extends TestCase {

    public function testSearchReturnsEmptyForDisabledVectorSync(): void {
        $client = $this->createMock(McpServerClient::class);
        $client->method('getStatus')->willReturn([
            'vector_sync_enabled' => false,
        ]);

        $provider = new SemanticSearchProvider(
            $client,
            $this->createMock(IL10N::class),
            $this->createMock(IURLGenerator::class),
        );

        $user = $this->createMock(IUser::class);
        $query = $this->createMock(ISearchQuery::class);
        $query->method('getTerm')->willReturn('test query');

        $result = $provider->search($user, $query);

        $this->assertEmpty($result->getEntries());
    }

    public function testSearchTransformsResults(): void {
        $client = $this->createMock(McpServerClient::class);
        $client->method('getStatus')->willReturn([
            'vector_sync_enabled' => true,
        ]);
        $client->method('search')->willReturn([
            'results' => [
                [
                    'doc_type' => 'note',
                    'title' => 'Test Note',
                    'excerpt' => 'This is a test...',
                    'score' => 0.85,
                    'id' => 123,
                ],
            ],
            'total_found' => 1,
        ]);

        $l10n = $this->createMock(IL10N::class);
        $l10n->method('t')->willReturnArgument(0);

        $urlGenerator = $this->createMock(IURLGenerator::class);
        $urlGenerator->method('linkToRoute')->willReturn('/apps/notes');
        $urlGenerator->method('imagePath')->willReturn('/apps/notes/img/app.svg');

        $provider = new SemanticSearchProvider($client, $l10n, $urlGenerator);

        $user = $this->createMock(IUser::class);
        $query = $this->createMock(ISearchQuery::class);
        $query->method('getTerm')->willReturn('test');
        $query->method('getLimit')->willReturn(20);

        $result = $provider->search($user, $query);
        $entries = $result->getEntries();

        $this->assertCount(1, $entries);
        $this->assertEquals('Test Note', $entries[0]->getTitle());
    }
}
```

## Related Documentation

### To Update
- `docs/installation.md` - Add NC PHP app installation section
- `docs/configuration.md` - Document management API settings and deployment modes
- `docs/authentication.md` - Document all three deployment modes (basic, basic_multiuser, oauth)
- `README.md` - Update architecture diagram with deployment mode decision tree

### To Create
- `docs/management-api.md` - API reference for NC app developers
- `docs/nextcloud-app-installation.md` - Step-by-step NC app setup
- `docs/migration-from-app-endpoint.md` - Guide for existing users
- `docs/development-nc-app.md` - Developer guide for NC app

### Related ADRs
- **ADR-011**: AppAPI Architecture (Rejected) - Explains why ExApp doesn't work
- **ADR-008**: MCP Sampling for Semantic Search - Requires standalone server
- **ADR-004**: Progressive Consent - OAuth architecture preserved

## Conclusion

Creating a Nextcloud PHP app for settings and management UI provides the best of both worlds:

**✅ Full MCP Protocol Support:**
- Standalone server preserves sampling, elicitation, streaming
- No ExApp limitations (ADR-011 findings)
- External MCP clients work unchanged
- All three deployment modes supported (basic, basic_multiuser, oauth)

**✅ Native Nextcloud Integration:**
- Settings in standard NC interface
- Single sign-on (NC sessions)
- Familiar UX for NC users
- Mobile and accessibility built-in

**✅ Flexible Authentication Architecture:**
- **Basic single-user**: Development and simple deployments
- **Basic multi-user**: Multi-user deployments without OIDC infrastructure
- **OAuth**: Production deployments with full security features

**✅ Clean Architecture:**
- Separation of concerns (protocol vs UI)
- Single source of truth (MCP server owns logic)
- Versioned API contract
- Independent scaling and deployment

This architecture solves the original goal: **Enable MCP sampling and advanced features while migrating the `/app` interface to a Nextcloud app**. The management API provides a clean integration point with authentication methods appropriate for each deployment scenario, and the gradual migration path ensures existing users aren't disrupted.

**Key Architectural Decision:** Multi-user support without OIDC is achieved via **BasicAuth pass-through mode** (deployment mode 2), where the MCP server extracts credentials from client requests and forwards them to Nextcloud for validation. This eliminates the need for SSE proxying in PHP while providing per-user identity and access control.
