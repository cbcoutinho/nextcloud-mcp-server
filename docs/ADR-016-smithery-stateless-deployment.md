# ADR-016: Smithery Stateless Deployment for Multi-User Public Nextcloud Instances

**Status:** Proposed
**Date:** 2025-01-22
**Deciders:** Development Team
**Related:** ADR-004 (OAuth), ADR-007 (Background Vector Sync), ADR-015 (Unified Provider)

## Context

[Smithery](https://smithery.ai) is a hosting platform and marketplace for MCP servers that provides:

- **Discovery**: Marketplace listing for MCP servers
- **Hosting**: Containerized deployment with auto-scaling
- **Authentication UI**: OAuth flow presentation for users
- **Session Configuration**: Per-user settings passed via URL parameters
- **Observability**: Usage logs and monitoring

### Current Architecture Limitations

The current nextcloud-mcp-server architecture assumes a **self-hosted deployment** with:

1. **Persistent Infrastructure**
   - Qdrant vector database for semantic search
   - Background sync worker for content indexing
   - Refresh token storage for offline access

2. **Single-Tenant Configuration**
   - Environment variables configure one Nextcloud instance
   - `NEXTCLOUD_HOST`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`
   - Or OAuth with a single IdP

3. **Stateful Operations**
   - Vector sync maintains index state across requests
   - Token storage persists between sessions

### Smithery Hosting Constraints

Smithery-hosted containers are **stateless by design**:

- No persistent storage between requests
- No background workers or cron jobs
- No databases (Qdrant, Redis, etc.)
- Containers may be recycled at any time
- Configuration passed per-session via URL parameters

### Opportunity

Many users have **publicly accessible Nextcloud instances** and want to:

1. Try the MCP server without self-hosting infrastructure
2. Connect multiple users to different Nextcloud instances
3. Use basic Nextcloud tools without semantic search
4. Benefit from Smithery's discovery and OAuth UI

## Decision

Implement a **stateless deployment mode** for Smithery that:

1. **Disables stateful features** (vector sync, semantic search)
2. **Creates clients per-session** from Smithery configuration
3. **Supports multiple Nextcloud instances** via session config
4. **Provides a useful subset of tools** that work without infrastructure

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Smithery-Hosted Stateless Mode                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  MCP Client                    Smithery                                  │
│  (Cursor, Claude)              Infrastructure                            │
│        │                            │                                    │
│        │ 1. Connect                 │                                    │
│        ├───────────────────────────►│                                    │
│        │                            │                                    │
│        │ 2. Config UI               │                                    │
│        │◄───────────────────────────┤  User enters:                      │
│        │    (Smithery presents)     │  - nextcloud_url                   │
│        │                            │  - auth_mode (basic/oauth)         │
│        │                            │  - credentials                     │
│        │ 3. Tool call               │                                    │
│        ├───────────────────────────►│                                    │
│        │    + session config        │                                    │
│        │                            │                                    │
│        │                    ┌───────┴───────┐                            │
│        │                    │  MCP Server   │                            │
│        │                    │  Container    │                            │
│        │                    │               │                            │
│        │                    │ 4. Create     │                            │
│        │                    │    client     │                            │
│        │                    │    from       │                            │
│        │                    │    config     │                            │
│        │                    │      │        │                            │
│        │                    │      ▼        │                            │
│        │                    │ 5. Call       │                            │
│        │                    │    Nextcloud  │───────► User's Nextcloud   │
│        │                    │    API        │         Instance           │
│        │                    │      │        │                            │
│        │                    │      ▼        │                            │
│        │ 6. Response        │ Return result │                            │
│        │◄───────────────────┤               │                            │
│        │                    └───────────────┘                            │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Session Configuration Schema

```python
from pydantic import BaseModel, Field

class SmitheryConfigSchema(BaseModel):
    """Configuration schema for Smithery session."""

    # Required: Nextcloud instance
    nextcloud_url: str = Field(
        ...,
        description="Your Nextcloud instance URL (e.g., https://cloud.example.com)"
    )

    # Authentication mode
    auth_mode: str = Field(
        "app_password",
        description="Authentication method: 'app_password' or 'oauth'"
    )

    # App Password authentication (recommended for Smithery)
    username: str | None = Field(
        None,
        description="Nextcloud username (required for app_password auth)"
    )
    app_password: str | None = Field(
        None,
        description="Nextcloud app password (Settings → Security → App passwords)"
    )

    # OAuth authentication (advanced)
    # When auth_mode='oauth', Smithery handles the OAuth flow
    # and passes the access token automatically
```

### Feature Matrix

| Feature | Self-Hosted | Smithery Stateless |
|---------|-------------|-------------------|
| **Notes** | | |
| List/Search notes | ✓ | ✓ |
| Get/Create/Update notes | ✓ | ✓ |
| Semantic search | ✓ | ✗ |
| **Calendar** | | |
| List calendars | ✓ | ✓ |
| Get/Create events | ✓ | ✓ |
| **Contacts** | | |
| List address books | ✓ | ✓ |
| Search/Get contacts | ✓ | ✓ |
| **Files (WebDAV)** | | |
| List/Download files | ✓ | ✓ |
| Upload files | ✓ | ✓ |
| Search files | ✓ | ✓ (keyword only) |
| **Deck** | | |
| List boards/cards | ✓ | ✓ |
| Create/Update cards | ✓ | ✓ |
| **Tables** | | |
| List/Query tables | ✓ | ✓ |
| Create/Update rows | ✓ | ✓ |
| **Cookbook** | | |
| List/Get recipes | ✓ | ✓ |
| **Semantic Search** | | |
| Vector search | ✓ | ✗ |
| RAG answers | ✓ | ✗ |
| **Background Sync** | | |
| Auto-indexing | ✓ | ✗ |
| Webhook sync | ✓ | ✗ |
| **Admin UI (`/app`)** | | |
| Vector sync status | ✓ | ✗ |
| Vector visualization | ✓ | ✗ |
| Webhook management | ✓ | ✗ |
| Session management | ✓ | ✗ |

### Implementation

#### 1. Deployment Mode Detection

```python
# nextcloud_mcp_server/config.py

class DeploymentMode(Enum):
    SELF_HOSTED = "self_hosted"      # Full features, env-based config
    SMITHERY_STATELESS = "smithery"  # Stateless, session-based config

def get_deployment_mode() -> DeploymentMode:
    """Detect deployment mode from environment."""
    if os.getenv("SMITHERY_DEPLOYMENT") == "true":
        return DeploymentMode.SMITHERY_STATELESS
    return DeploymentMode.SELF_HOSTED
```

#### 2. Session-Based Client Factory

```python
# nextcloud_mcp_server/context.py

async def get_client(ctx: Context) -> NextcloudClient:
    """Get NextcloudClient - from session config or environment."""

    mode = get_deployment_mode()

    if mode == DeploymentMode.SMITHERY_STATELESS:
        # Create client from Smithery session config
        config = ctx.session_config
        if not config:
            raise McpError("Session configuration required")

        return NextcloudClient(
            base_url=config.nextcloud_url,
            username=config.username,
            password=config.app_password,
        )
    else:
        # Existing behavior: from environment or OAuth context
        return await _get_client_from_context(ctx)
```

#### 3. Conditional Tool Registration

```python
# nextcloud_mcp_server/app.py

def create_mcp_server(mode: DeploymentMode) -> FastMCP:
    """Create MCP server with mode-appropriate tools."""

    mcp = FastMCP("Nextcloud MCP")

    # Always register core tools
    configure_notes_tools(mcp)
    configure_calendar_tools(mcp)
    configure_contacts_tools(mcp)
    configure_webdav_tools(mcp)
    configure_deck_tools(mcp)
    configure_tables_tools(mcp)
    configure_cookbook_tools(mcp)

    # Only register stateful tools in self-hosted mode
    if mode == DeploymentMode.SELF_HOSTED:
        configure_semantic_tools(mcp)  # Requires Qdrant
        register_oauth_tools(mcp)       # Requires token storage

    return mcp
```

#### 4. Exclude Admin UI Routes

The `/app` admin UI should **not be installed** in Smithery mode because:

- **Vector sync status** - No vector sync in stateless mode
- **Vector visualization** - No Qdrant to visualize
- **Webhook management** - No webhook sync without background workers
- **Session management** - No persistent sessions to manage

```python
# nextcloud_mcp_server/app.py

def create_app(mode: DeploymentMode) -> Starlette:
    """Create Starlette app with mode-appropriate routes."""

    routes = [
        Route("/health/live", health_live, methods=["GET"]),
        Route("/health/ready", health_ready, methods=["GET"]),
    ]

    # Only mount admin UI in self-hosted mode
    if mode == DeploymentMode.SELF_HOSTED:
        browser_app = create_browser_app()
        routes.append(
            Route("/app", lambda r: RedirectResponse("/app/", status_code=307))
        )
        routes.append(Mount("/app", app=browser_app))
        logger.info("Admin UI mounted at /app")
    else:
        logger.info("Admin UI disabled in Smithery stateless mode")

    # Mount FastMCP at root
    mcp_app = create_mcp_server(mode).streamable_http_app()
    routes.append(Mount("/", app=mcp_app))

    return Starlette(routes=routes, lifespan=starlette_lifespan)
```

**Endpoints by Mode:**

| Endpoint | Self-Hosted | Smithery |
|----------|-------------|----------|
| `/mcp` | ✓ | ✓ |
| `/health/live` | ✓ | ✓ |
| `/health/ready` | ✓ | ✓ |
| `/.well-known/mcp-config` | ✓ | ✓ |
| `/app` | ✓ | ✗ |
| `/app/vector-sync/status` | ✓ | ✗ |
| `/app/vector-viz` | ✓ | ✗ |
| `/app/webhooks` | ✓ | ✗ |

#### 5. Smithery Integration Files

**smithery.yaml:**
```yaml
runtime: "container"
build:
  dockerfile: "Dockerfile.smithery"
  dockerBuildPath: "."
startCommand:
  type: "http"
  configSchema:
    type: "object"
    required: ["nextcloud_url", "username", "app_password"]
    properties:
      nextcloud_url:
        type: "string"
        title: "Nextcloud URL"
        description: "Your Nextcloud instance URL (e.g., https://cloud.example.com)"
      username:
        type: "string"
        title: "Username"
        description: "Your Nextcloud username"
      app_password:
        type: "string"
        title: "App Password"
        description: "Generate at Settings → Security → App passwords"
  exampleConfig:
    nextcloud_url: "https://cloud.example.com"
    username: "alice"
    app_password: "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"
```

**Dockerfile.smithery:**
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY nextcloud_mcp_server ./nextcloud_mcp_server

# Install dependencies (without vector/semantic extras)
RUN uv sync --frozen --no-dev

# Set Smithery mode
ENV SMITHERY_DEPLOYMENT=true
ENV VECTOR_SYNC_ENABLED=false

# Smithery sets PORT=8081
EXPOSE 8081

CMD ["uv", "run", "python", "-m", "nextcloud_mcp_server.smithery_main"]
```

**nextcloud_mcp_server/smithery_main.py:**
```python
"""Smithery-specific entrypoint for stateless deployment."""

import os
import uvicorn
from starlette.middleware.cors import CORSMiddleware

from nextcloud_mcp_server.app import create_mcp_server
from nextcloud_mcp_server.config import DeploymentMode

def main():
    # Force stateless mode
    os.environ["SMITHERY_DEPLOYMENT"] = "true"
    os.environ["VECTOR_SYNC_ENABLED"] = "false"

    mcp = create_mcp_server(DeploymentMode.SMITHERY_STATELESS)
    app = mcp.streamable_http_app()

    # Add CORS for browser-based clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id", "mcp-protocol-version"],
    )

    # Smithery sets PORT environment variable
    port = int(os.environ.get("PORT", 8081))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
```

### Security Considerations

1. **App Passwords over User Passwords**
   - Smithery config encourages app passwords (revocable, scoped)
   - Documentation guides users to create dedicated app passwords
   - App passwords can be revoked without changing main password

2. **HTTPS Required**
   - `nextcloud_url` must be HTTPS for production use
   - Validation rejects HTTP URLs in Smithery mode

3. **No Credential Storage**
   - Credentials exist only for request duration
   - No server-side persistence of user credentials
   - Smithery handles secure config transmission

4. **Scope Limitation**
   - Stateless mode cannot access offline_access
   - No background operations on user's behalf
   - Clear user expectation: tools work during session only

### Migration Path

Users can start with Smithery stateless mode and migrate to self-hosted:

1. **Try on Smithery** → Basic tools, no setup
2. **Self-host for semantic search** → Add Qdrant, enable vector sync
3. **Full deployment** → Background sync, webhooks, multi-user OAuth

## Consequences

### Positive

1. **Lower barrier to entry** - Users can try without infrastructure
2. **Multi-user support** - Each session connects to different Nextcloud
3. **Smithery ecosystem** - Discovery, observability, OAuth UI
4. **Clear feature tiers** - Stateless (simple) vs self-hosted (full)

### Negative

1. **No semantic search** - Key differentiator unavailable on Smithery
2. **Per-request auth** - Credentials sent with each request
3. **No offline access** - Cannot perform background operations
4. **Maintenance burden** - Two deployment modes to support

### Neutral

1. **Feature subset** - May encourage users to self-host for full features
2. **Documentation needs** - Clear guidance on mode differences required

## Alternatives Considered

### 1. External MCP Only

**Approach:** Only support self-hosted external MCP registration on Smithery.

**Rejected because:**
- Higher barrier to entry for new users
- Misses opportunity for Smithery marketplace visibility
- Users want to try before committing to infrastructure

### 2. Embedded Vector DB (SQLite-vec)

**Approach:** Use SQLite with vector extensions for per-request indexing.

**Rejected because:**
- No persistence between requests anyway
- Indexing latency too high for synchronous requests
- Complexity without benefit in stateless context

### 3. External Vector DB Service

**Approach:** Connect to Pinecone/Weaviate Cloud from Smithery container.

**Rejected because:**
- Adds external dependency and cost
- Per-user collections require complex multi-tenancy
- Sync still impossible without background workers

### 4. Hybrid: Smithery + User's Qdrant

**Approach:** User provides their own Qdrant URL in session config.

**Considered for future:**
- Could enable semantic search for advanced users
- Adds complexity to session config
- Sync still requires external trigger (manual or webhook)

## References

- [Smithery Documentation](https://smithery.ai/docs)
- [Smithery Session Configuration](https://smithery.ai/docs/build/session-config)
- [Smithery External MCPs](https://smithery.ai/docs/build/external)
- [MCP Streamable HTTP Transport](https://modelcontextprotocol.io/docs/concepts/transports)
- [Nextcloud App Passwords](https://docs.nextcloud.com/server/latest/user_manual/en/session_management.html#app-passwords)
