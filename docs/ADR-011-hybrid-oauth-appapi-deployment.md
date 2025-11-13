# ADR-011: Hybrid OAuth and AppAPI Deployment Architecture

**Status**: Not Planned
**Date**: 2025-01-13 (Initial), 2025-01-13 (Rejected)
**Related**: ADR-004 (Progressive Consent), ADR-005 (Token Audience Validation)

## Decision Outcome

After comprehensive research and analysis, **this hybrid architecture is NOT being implemented**. The investigation revealed fundamental architectural incompatibilities between MCP's protocol requirements and AppAPI's request/response proxy model that cannot be resolved without significant upstream changes to AppAPI. The limitations are too severe to provide a viable user experience for MCP's core features.

## Context

The Nextcloud MCP Server currently implements a sophisticated OAuth 2.0 / OIDC authentication architecture (ADR-004, ADR-005) that supports multi-tenant deployments, fine-grained scope-based permissions, and progressive consent. This architecture works well for standalone MCP server deployments where the server operates as a separate service from Nextcloud, requiring users to explicitly authenticate and authorize access to their Nextcloud data.

However, this OAuth-based approach has limitations for certain deployment scenarios:

**Deployment Complexity**: Setting up the OAuth flow requires:
- Configuring an identity provider (Keycloak, Nextcloud's built-in OIDC, etc.)
- Registering OAuth clients via Dynamic Client Registration (DCR)
- Configuring redirect URIs, token endpoints, and JWKS endpoints
- Setting up token storage for refresh tokens
- Managing token lifecycle (refresh, expiration, revocation)

For administrators who simply want to enable MCP functionality within their existing Nextcloud instance, this setup burden is significant—especially compared to traditional Nextcloud apps that can be installed with a single click.

**Integration Limitations**: The current architecture treats Nextcloud as an external API, accessed via HTTP with bearer token authentication. This prevents the MCP server from:
- Registering UI components in Nextcloud (menu items, file actions, dashboard widgets)
- Responding to Nextcloud events and webhooks natively
- Leveraging Nextcloud's built-in permission system
- Being managed via Nextcloud's app management interface

**Single-Tenant Overhead**: For single-tenant deployments (one Nextcloud instance, one MCP server), the OAuth architecture's multi-tenant capabilities (separate tokens per user, per-user consent) add complexity without providing value. The admin already trusts the MCP server by deploying it—explicit per-user consent becomes redundant.

### Nextcloud AppAPI (ExApp) Architecture

Nextcloud provides an **AppAPI** framework for running **External Applications (ExApps)**—applications that run outside the Nextcloud PHP process but integrate deeply with Nextcloud. ExApps are the evolution of traditional PHP-based Nextcloud apps, designed to support multiple programming languages (Python, Go, etc.) and run in separate containers for enhanced security and stability.

**Key AppAPI Characteristics**:

1. **Shared Secret Authentication**: ExApps use a permanent shared secret instead of OAuth tokens. Nextcloud passes user context via the `AUTHORIZATION-APP-API` header: `base64(userId:secret)`.

2. **Built-in Proxy**: AppAPI provides a production-ready HTTP proxy at `/index.php/apps/app_api/proxy/{appId}/{path}` that:
   - Routes external requests through Nextcloud to the ExApp
   - Translates Nextcloud user sessions into authenticated ExApp requests
   - Supports HTTP streaming (including Server-Sent Events for MCP)
   - Enforces access control (PUBLIC, USER, ADMIN levels)

3. **Native Integration**: ExApps can register:
   - Top menu items
   - File actions (right-click context menus)
   - Dashboard widgets
   - Settings pages
   - Background jobs (cron tasks)
   - Event listeners

4. **Admin-Controlled Permissions**: Scopes are granted at installation time by the administrator (via `info.xml` manifest), not per-user. Users inherit the app's permissions based on their Nextcloud account privileges.

5. **Container Deployment**: ExApps run as Docker containers managed by Nextcloud's daemon configuration, with automatic lifecycle management (start, stop, update, uninstall).

**Example ExApp Deployment Flow**:
```bash
# Admin registers ExApp via occ command
php occ app_api:app:register nextcloud_mcp_server \
  --json-info '{"id":"nextcloud_mcp_server","secret":"<generated>",...}'

# OR: One-click install from Nextcloud app store (future)
```

**MCP Client Connection via Proxy**:
```
User → Nextcloud (session auth) → AppAPI Proxy → ExApp MCP Server
```

The proxy handles user authentication and injects the `AUTHORIZATION-APP-API` header, so the ExApp receives pre-authenticated requests with user context.

### Why Both Architectures Are Valuable

**OAuth Mode Best For**:
- **Multi-tenant SaaS**: One MCP server instance serving multiple Nextcloud instances
- **External MCP clients**: Clients not integrated with Nextcloud (Claude Desktop, custom clients)
- **Fine-grained consent**: Users explicitly authorize which scopes the MCP client can access
- **Per-user token management**: Background jobs that require long-lived refresh tokens
- **Standalone services**: MCP server as a separate service with its own lifecycle

**AppAPI Mode Best For**:
- **Single-tenant deployments**: One Nextcloud instance with integrated MCP functionality
- **Simplified administration**: One-click installation via Nextcloud app store
- **Native Nextcloud integration**: UI components, event listeners, dashboard widgets
- **Admin-controlled permissions**: Trust model where admin installation implies consent
- **Nextcloud-managed lifecycle**: Automatic container management and updates

Both deployment models serve legitimate use cases. Rather than choosing one over the other, a **hybrid architecture** supporting both modes maximizes flexibility and serves the broadest user base.

### Design Challenges

**Code Duplication Risk**: Implementing two separate authentication mechanisms could lead to massive code duplication—separate tool implementations, separate clients, separate tests—doubling maintenance burden.

**Abstraction Complexity**: Creating an abstraction layer that works seamlessly for both OAuth (token-based, per-user scopes) and AppAPI (header-based, admin-granted permissions) risks over-engineering and introducing bugs.

**Dependency Management**: OAuth mode requires `authlib`, `pyjwt`, `aiosqlite`. AppAPI mode requires `nc_py_api`. Installing both adds bloat; making them mutually exclusive complicates development.

**Testing Burden**: Supporting both modes doubles the test matrix—each integration test must pass in both OAuth and AppAPI configurations.

**Documentation Complexity**: Users need clear guidance on when to use each mode, how to configure it, and how they differ in behavior.

Despite these challenges, the value proposition—supporting both standalone OAuth deployments and native Nextcloud integration—justifies the architectural investment.

## AppAPI Limitations and Challenges

While AppAPI provides valuable benefits for Nextcloud integration, research has identified several technical limitations that constrain the MCP server's capabilities in AppAPI mode compared to OAuth mode. These limitations stem from fundamental architectural differences between the two approaches.

### Authentication Challenges

**Session Cookie Dependency**: The AppAPI proxy relies on Nextcloud user sessions for authentication. When a user accesses `/index.php/apps/app_api/proxy/{appId}/{path}`, the proxy extracts the user ID from their Nextcloud session cookie and converts it to the `AUTHORIZATION-APP-API` header.

**Non-Browser Client Problem**: MCP clients like Claude Desktop are non-browser applications that cannot:
- Handle browser-based session cookies
- Interact with Nextcloud login forms
- Complete standard OAuth browser flows

**Workaround Required**: To support non-browser MCP clients in AppAPI mode, one of the following solutions is needed:

1. **App Passwords** (Simplest): Add BasicAuth support to the AppAPI proxy. Users generate app-specific passwords in Nextcloud's security settings, and MCP clients send `Authorization: Basic base64(username:appPassword)`. The proxy validates credentials and converts to `AUTHORIZATION-APP-API` headers. Requires upstream AppAPI modification.

2. **OAuth 2.0 Integration** (Most Secure): Integrate MCP's OAuth client with Nextcloud's OIDC provider. MCP clients obtain access tokens via standard OAuth flow, include `Authorization: Bearer <token>` headers, and the proxy validates tokens via introspection. Requires significant proxy modification and token caching for performance.

3. **PUBLIC Routes with Internal Auth** (Bypass Proxy): Register ExApp routes as PUBLIC (no authentication) and implement validation within the ExApp. Less secure and duplicates authentication logic.

**Recommendation**: Implement app password support (Option 1) as Phase 1, with OAuth 2.0 (Option 2) as a long-term production-ready solution.

### Streaming Limitations

**Buffered, Not Real-Time**: The AppAPI proxy supports HTTP streaming via Guzzle's `stream: true` option, which efficiently handles large response bodies. However, the streaming is **buffered**, not incremental:

- ExApp generates response chunks
- Guzzle streams response to proxy (low memory usage)
- Proxy accumulates complete response
- Client receives response only after completion

**SSE Response Handling**: While ExApps can return `Content-Type: text/event-stream` responses, the events arrive at the client as a complete buffered stream, not as individual events in real-time. This works for bounded operations (finite event streams) but not for true real-time streaming.

**No WebSocket Support**: The proxy does not support WebSocket protocol upgrades. WebSocket requires:
- HTTP/1.1 Upgrade protocol handling
- Different HTTP client library (Guzzle doesn't support WebSocket)
- Persistent bidirectional connection management
- Significant proxy architectural changes

**Impact on MCP Transports**:

| Transport | AppAPI Support | Streaming | Usability |
|-----------|---------------|-----------|-----------|
| **Streamable HTTP** | ✅ Buffered | Bounded | **Works today** |
| **stdio** | ❌ Future | True | Ideal but requires AppAPI changes |
| **SSE (legacy)** | ⚠️ Buffered | Bounded | Deprecated by MCP |
| **WebSocket** | ❌ Major rewrite | True | Not viable |

**Recommendation**: Use Streamable HTTP transport (MCP 2025-03-26+), which works with current AppAPI proxy. Operations complete as bounded streams. For true real-time streaming, advocate for stdio transport support in AppAPI (requires `docker exec -i` integration, estimated 2-4 weeks development).

### Notification and Progress Update Limitations

**MCP's Notification Model**: The MCP protocol supports server-to-client notifications over persistent connections (SSE or WebSocket). This enables:
- Progress updates during long-running operations
- Sampling requests (LLM generation via client)
- Asynchronous events from server to client

**AppAPI's Request/Response Architecture**: The AppAPI proxy operates on a request/response model:
- Client sends HTTP request through proxy
- ExApp processes and returns response
- Connection closes
- No persistent connection to original client
- ExApp doesn't know who the client is (only sees proxy)

**Feature Incompatibility**:

| Feature | OAuth/SSE Mode | AppAPI Mode | Notes |
|---------|---------------|-------------|-------|
| Tool execution | ✅ Immediate | ✅ Immediate | Works fine |
| Progress updates | ✅ Real-time via MCP | ❌ No channel | Must use Nextcloud notifications |
| Sampling (LLM/RAG) | ✅ Via MCP protocol | ❌ No protocol forwarding | **Critical limitation** |
| Long operations | ✅ With incremental progress | ⚠️ Background tasks only | Different pattern required |
| Bidirectional events | ✅ Via persistent connection | ❌ Request/response only | Architectural incompatibility |

**Workarounds for AppAPI Mode**:

1. **Short Operations (<30s)**: Direct request/response works fine. No changes needed.

2. **Long Operations**: Use FastAPI `BackgroundTasks` + Nextcloud notifications:
   ```python
   @mcp.tool()
   async def long_operation(ctx: NextcloudApp, background: BackgroundTasks):
       task_id = uuid.uuid4()
       background.add_task(process_long_operation, ctx, task_id)
       return {"status": "accepted", "task_id": task_id}

   def process_long_operation(nc: NextcloudApp, task_id: str):
       result = do_work()
       nc.notifications.create("Task completed", f"Result: {result}")
   ```

3. **Sampling/LLM Features**: **Not possible in AppAPI mode**. The MCP sampling protocol (`ctx.session.create_message()`) requires bidirectional communication with the client. Tools using sampling must detect the deployment mode and gracefully degrade:
   ```python
   @mcp.tool()
   async def semantic_search_answer(query: str, ctx: Context | NextcloudApp):
       if isinstance(ctx, NextcloudApp):
           # AppAPI mode: return documents only, no LLM generation
           return SearchResponse(results=documents, message="LLM generation not available in AppAPI mode")
       else:
           # OAuth mode: use sampling for LLM answer
           answer = await ctx.session.create_message(...)
           return SamplingResponse(answer=answer, sources=documents)
   ```

**Recommendation**: Document feature limitations clearly. Disable sampling-based tools in AppAPI mode with helpful error messages. Use Nextcloud's notification system for progress updates on long-running operations.

### Webhook and Callback Support

**Outbound Webhooks (Nextcloud → ExApp)**: ✅ **Well Supported**

ExApps can register webhooks via the `webhooks_listener` app:
```python
nc.webhooks.register(
    http_method="POST",
    uri="/webhook/file_created",
    event="OCP\\Files\\Events\\Node\\NodeCreatedEvent",
    event_filter={...}
)
```

**Inbound Notifications (ExApp → Nextcloud)**: ✅ **Supported**

ExApps can send notifications to users:
```python
nc.notifications.create(
    subject="Task completed",
    message="Your file is ready",
    link=result_url
)
```

**Limitation**: ExApps cannot send MCP protocol notifications back through the proxy to the original MCP client. Communication is ExApp → Nextcloud → User UI, not ExApp → MCP Client.

### Validation: Nextcloud Context Agent as Real-World Example

The limitations documented above are not theoretical - they are **validated by Nextcloud's own Context Agent project**, which is an AppAPI ExApp that exposes MCP functionality and faces identical constraints.

#### Context Agent Architecture

**Context Agent** (`~/Software/context_agent/`) is an official Nextcloud project that demonstrates the ExApp architecture in practice:

**Type**: AppAPI External App (ExApp) written in Python with FastMCP

**Dual MCP Role**:
1. **MCP Server**: Exposes ~28 tools (calendar, contacts, files, talk, mail, deck) via `/mcp` endpoint
2. **MCP Client**: Consumes external MCP servers using `langchain-mcp-adapters`

**Route Configuration** (`appinfo/info.xml`):
```xml
<route>
  <url>mcp</url>
  <verb>POST,GET,DELETE</verb>
  <access_level>USER</access_level>
</route>
```

**Access Pattern**: External MCP clients connect via AppAPI proxy:
```
MCP Client → /apps/app_api/proxy/context_agent/mcp → Context Agent ExApp
```

#### Context Agent Faces Identical Limitations

Despite being an official Nextcloud project with MCP integration, Context Agent has **exactly the same AppAPI proxy limitations** documented in this ADR:

**✅ What Works**:
- Basic MCP tools (request → response pattern)
- Tool listing and discovery
- Stateless HTTP transport
- User authentication via AppAPI headers

**❌ What Doesn't Work** (identical to our findings):
- MCP sampling (LLM completion requests)
- MCP elicitation (user input prompts)
- Real-time progress updates
- Bidirectional streaming communication
- Server-initiated notifications via MCP protocol

#### Context Agent's Workaround Strategy

Context Agent successfully provides agent functionality by **working around the MCP protocol limitations** rather than relying on them:

**Primary Use Case**: In-app AI agent called by **Nextcloud Assistant** (native PHP app)
- Uses Nextcloud's **Task Processing API** for orchestration (not external MCP clients)
- Implements custom confirmation flow via Assistant UI (not MCP elicitation)
- Uses LangGraph state machine for agent logic
- Leverages Nextcloud APIs for user interaction

**Confirmation Flow Example** (`ex_app/lib/agent.py:105-117`):
```python
if state_snapshot.next == ('dangerous_tools', ):
    if task['input']['confirmation'] == 0:
        # User denied via Nextcloud UI - return denial message
        return ToolMessage(
            tool_call_id=tool_call["id"],
            content=f"API call denied by user. Reasoning: '{task['input']['input']}'"
        )
    else:
        # User approved via Nextcloud UI - execute tool
        execute_tool()
```

**Flow**:
1. Agent queues dangerous tool call
2. Returns to Assistant with `actions` field (outside MCP)
3. **Assistant UI prompts user** for confirmation in Nextcloud
4. User approves/denies in Nextcloud interface
5. New task submitted with `confirmation` field
6. Agent proceeds based on confirmation

**Key Insight**: Context Agent **does NOT use MCP's native elicitation** - it implements a completely custom confirmation flow through Nextcloud's Task Processing API because MCP elicitation is impossible through the AppAPI proxy.

#### Architecture Comparison

```
Context Agent Primary Use (Works):
Nextcloud Assistant UI → Task Processing API → Context Agent ExApp → Tools
                        ✅ Custom confirmation via NC APIs (not MCP)

Context Agent MCP Endpoint (Limited):
External MCP Client → AppAPI Proxy → Context Agent /mcp → Tools
                     ❌ Sampling blocked (same as our server)

Our MCP Server Use Case (Blocked):
External MCP Client → AppAPI Proxy → Our ExApp → Nextcloud APIs
                     ❌ Sampling blocked (same limitation)
```

#### Why Context Agent Succeeds Despite Limitations

Context Agent works because:
1. **Different primary use case**: In-app integration via Task Processing API, not external MCP clients
2. **Custom workarounds**: Uses Nextcloud APIs for features MCP can't provide through proxy
3. **Accepts limitations**: MCP endpoint is secondary feature with documented constraints
4. **Alternative protocols**: Uses Task Processing API as primary interface, MCP as optional

#### Implications for Our MCP Server

Context Agent validates that:
1. **AppAPI proxy limitations are architectural**, not implementation-specific
2. **All ExApps face identical constraints** - even official Nextcloud projects
3. **Workarounds exist** but require using Nextcloud APIs outside MCP protocol
4. **Different use cases require different approaches**:
   - In-app integration: Use Task Processing API (like Context Agent)
   - External MCP clients: Require OAuth mode (no viable AppAPI solution)

**If our MCP server's primary use case is external MCP clients** (Claude Desktop, custom clients), AppAPI mode provides no viable path forward. The limitations eliminate core MCP features (sampling/RAG, real-time progress) that are essential for external client integration.

**If targeting in-app integration** (like Context Agent), we would need to:
- Register as Task Processing provider
- Implement custom confirmation via Assistant UI
- Use Nextcloud APIs for user interaction
- Accept that MCP protocol features are unavailable

This represents a fundamentally different product with different capabilities and user experience.

#### Conclusion from Context Agent Analysis

The existence of Context Agent with identical limitations **strengthens the case against AppAPI mode** for our use case:

- Official Nextcloud project faces same constraints
- Successfully works around them by changing use case and protocol
- Confirms limitations are inherent to ExApp architecture
- Demonstrates that MCP protocol features cannot work through AppAPI proxy

**Our recommendation**: Continue with OAuth mode for external MCP clients, do not pursue AppAPI mode unless the product vision shifts to in-app integration via Task Processing API.

## Decision

**We will NOT implement AppAPI mode** for external MCP client integration. The project will continue with OAuth mode exclusively.

### Rationale

After comprehensive research including analysis of Nextcloud's own Context Agent project, the limitations of AppAPI ExApp architecture for MCP integration are too severe to provide acceptable user experience:

**Critical Missing Features in AppAPI Mode**:
1. **No MCP sampling** - Eliminates RAG/LLM generation features (ADR-008)
2. **No real-time progress** - Breaks user experience for long-running operations
3. **No bidirectional streaming** - Core MCP protocol features unusable
4. **Buffered-only streaming** - Defeats the purpose of streaming protocols

**These are not implementation challenges** - they are fundamental architectural incompatibilities between:
- **MCP's requirements**: Multi-turn nested interactions, server-initiated requests, bidirectional streaming
- **AppAPI's architecture**: Stateless request/response proxy, no persistent connections, no message routing

**Validation from Context Agent**: Nextcloud's official MCP-enabled ExApp faces identical limitations and works around them by:
- Using Task Processing API instead of MCP protocol for user interaction
- Targeting in-app Assistant integration, not external MCP clients
- Accepting that MCP endpoint is secondary with documented constraints

### Why OAuth Mode Remains Sole Solution

OAuth mode provides all essential MCP features:
- ✅ Full MCP protocol support (sampling, elicitation, streaming)
- ✅ Real-time progress updates
- ✅ Multi-tenant capability
- ✅ External client integration (Claude Desktop, custom clients)
- ✅ Fine-grained per-user permissions
- ✅ Proven architecture (production-ready)

### Alternative Considered: Task Processing Provider

If in-app Nextcloud integration is desired in the future, the correct approach would be:
- Register as **Task Processing provider** (like Context Agent)
- Use **Nextcloud Assistant UI** for user interaction
- Implement custom flows via **Task Processing API**
- Accept that this is a **different product** with different capabilities

This would be a separate feature, not a replacement for external MCP client support.

### Original Hybrid Architecture (Documented for Reference)

The following sections document the hybrid architecture that was researched but not implemented. They are preserved for reference and to document why this approach was rejected.

#### Core Principles (Not Implemented)

1. **Single Codebase**: One repository, one Docker image build process, mode selection via environment variables
2. **Maximum Code Sharing**: 100% sharing of MCP tool implementations, client libraries, and business logic
3. **Backwards Compatibility**: OAuth mode remains the default; existing deployments continue working unchanged
4. **Mode Detection**: Runtime mode selection via `APPAPI_MODE=true` environment variable
5. **Abstraction Over Duplication**: Unified client interface that tools call regardless of mode
6. **Graceful Degradation**: Features that only work in one mode (e.g., UI registration in AppAPI) degrade gracefully

### Architecture Overview

```
┌────────────────────────────────────────────────────────────┐
│  MCP Tool Layer (100% Shared)                              │
│  - nc_notes_create_note()                                  │
│  - nc_calendar_get_events()                                │
│  - nc_semantic_search()                                    │
│  └─ Calls: get_client(ctx) → NextcloudClient              │
└────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────┐
│  Client Abstraction Layer (New)                            │
│  - get_client(ctx: Context | NextcloudApp)                 │
│  - Returns NextcloudClient regardless of mode              │
└────────────────────────────────────────────────────────────┘
                           │
          ┌────────────────┴────────────────┐
          ▼                                 ▼
┌──────────────────────┐        ┌──────────────────────┐
│  OAuth Mode          │        │  AppAPI Mode         │
│  - Token validation  │        │  - Header validation │
│  - Token refresh     │        │  - User context from │
│  - DCR               │        │    AUTHORIZATION-    │
│  - Scope checking    │        │    APP-API header    │
└──────────────────────┘        └──────────────────────┘
          │                                 │
          ▼                                 ▼
┌──────────────────────┐        ┌──────────────────────┐
│  NextcloudClient     │        │  NextcloudClient     │
│  (token-based HTTP)  │        │  (via nc_py_api)     │
└──────────────────────┘        └──────────────────────┘
```

### File Structure

```
nextcloud_mcp_server/
├── app.py                          # MODIFIED: Mode detection and delegation
├── app_oauth.py                    # NEW: OAuth-specific FastMCP setup
├── app_exapp.py                    # NEW: AppAPI-specific FastMCP setup
├── auth/
│   ├── bearer_auth.py              # OAuth token verification (existing)
│   ├── appapi_auth.py              # NEW: AppAPI header validation
│   ├── client_factory.py           # NEW: Mode-aware client abstraction
│   ├── scope_authorization.py      # MODIFIED: Mode-aware scope checking
│   └── context_helper.py           # MODIFIED: Support both Context and NextcloudApp
├── client/                         # NO CHANGES (100% shared)
│   ├── notes.py
│   ├── calendar.py
│   └── ...
├── server/                         # NO CHANGES (100% shared)
│   ├── notes.py
│   ├── calendar.py
│   └── ...
└── models/                         # NO CHANGES (100% shared)

appinfo/                            # NEW: ExApp manifest
├── info.xml                        # ExApp metadata and permissions
└── routes.xml                      # Proxy route configuration
```

### Feature Parity Matrix

The following table documents feature support and differences between OAuth and AppAPI deployment modes. Understanding these differences is critical for users choosing a deployment mode.

| Feature Category | Feature | OAuth Mode | AppAPI Mode | Notes |
|-----------------|---------|-----------|-------------|-------|
| **Authentication** | | | | |
| | Multi-user support | ✅ Per-user tokens | ✅ Per-request user context | Both support multiple users |
| | Non-browser clients | ✅ OAuth flow | ⚠️ Requires app passwords | AppAPI needs BasicAuth support |
| | Token management | ✅ Refresh tokens | N/A Shared secret | OAuth uses refresh tokens |
| | Fine-grained scopes | ✅ Per-user consent | ⚠️ Admin-granted | AppAPI scopes set at install |
| | Token revocation | ✅ Per-user | ⚠️ Uninstall app | Different security models |
| **Core Functionality** | | | | |
| | Tool execution | ✅ Full support | ✅ Full support | All MCP tools work |
| | Resource queries | ✅ Full support | ✅ Full support | No functional differences |
| | Error handling | ✅ Full support | ✅ Full support | Same error reporting |
| **Streaming & Communication** | | | | |
| | HTTP streaming | ✅ Real-time | ⚠️ Buffered | AppAPI accumulates complete response |
| | SSE transport | ✅ Real-time events | ⚠️ Buffered stream | AppAPI receives all events at once |
| | Streamable HTTP | ✅ Real-time | ⚠️ Buffered | Works but not incremental |
| | WebSocket | ⚠️ Not implemented | ❌ Not possible | Neither mode supports currently |
| | stdio transport | ❌ Not applicable | ⚠️ Future possibility | Would require AppAPI changes |
| **Progress & Notifications** | | | | |
| | Progress updates | ✅ Real-time via MCP | ❌ No channel | **Critical difference** |
| | MCP notifications | ✅ Server → Client | ❌ Impossible | No persistent connection |
| | Nextcloud notifications | N/A | ✅ ExApp → NC UI | AppAPI advantage |
| | Long-running ops | ✅ With progress | ⚠️ Background + notification | Different UX patterns |
| **Advanced Features** | | | | |
| | Sampling (LLM/RAG) | ✅ Full support | ❌ Not possible | **Critical limitation** |
| | Background sync workers | ✅ With refresh tokens | ⚠️ Via NC cron | Different mechanisms |
| | Vector database sync | ✅ Background workers | ⚠️ Webhook-driven | Different sync strategies |
| | Semantic search | ✅ With sampling | ⚠️ Without sampling | Limited in AppAPI mode |
| **Integration** | | | | |
| | Nextcloud UI components | ❌ External service | ✅ Native integration | AppAPI advantage |
| | File actions | ❌ Not possible | ✅ Right-click menus | AppAPI advantage |
| | Dashboard widgets | ❌ Not possible | ✅ Native widgets | AppAPI advantage |
| | Event listeners | ⚠️ Via polling | ✅ Via webhooks | AppAPI advantage |
| | Settings page | ❌ Separate UI | ✅ NC settings | AppAPI advantage |
| **Deployment** | | | | |
| | Installation | ⚠️ Manual OAuth setup | ✅ One-click install | AppAPI advantage |
| | Multi-tenant | ✅ One server, many NC instances | ❌ Single NC instance | OAuth advantage |
| | Standalone deployment | ✅ Independent service | ❌ Requires Nextcloud | OAuth advantage |
| | Container management | Manual | ✅ NC-managed | AppAPI advantage |
| | Updates | Manual | ✅ Via app store | AppAPI advantage |
| **Development & Testing** | | | | |
| | Local development | ✅ Simple setup | ⚠️ Requires NC instance | OAuth easier for dev |
| | Testing infrastructure | ✅ Well established | ⚠️ Needs work | OAuth tests mature |
| | Debug tooling | ✅ Standard HTTP | ⚠️ Via proxy | OAuth simpler |

**Legend:**
- ✅ **Full Support**: Feature works as expected with no limitations
- ⚠️ **Partial/Different**: Feature works but with limitations or different implementation
- ❌ **Not Supported**: Feature not available or not possible
- N/A **Not Applicable**: Feature doesn't apply to this mode

**Key Takeaways:**

1. **OAuth mode is best for**:
   - Multi-tenant SaaS deployments
   - External MCP client access (Claude Desktop, custom clients)
   - Features requiring sampling/LLM generation (RAG)
   - Real-time progress updates
   - Standalone service deployments

2. **AppAPI mode is best for**:
   - Single-tenant Nextcloud integration
   - Simplified installation and management
   - Native UI components and event listeners
   - Admin-controlled deployments
   - Nextcloud app store distribution

3. **Critical limitations in AppAPI mode**:
   - No sampling support (RAG features unavailable)
   - No real-time progress updates (use Nextcloud notifications instead)
   - Buffered streaming only (not true real-time)
   - Requires app passwords for non-browser clients (needs AppAPI enhancement)

### Implementation Details

#### 1. Mode Detection (app.py)

```python
import os
from typing import Literal

def get_deployment_mode() -> Literal["oauth", "exapp"]:
    """Detect deployment mode from environment."""
    if os.getenv("APPAPI_MODE") == "true":
        return "exapp"
    return "oauth"  # Default for backwards compatibility

def get_app(transport: str = "sse", enabled_apps: list[str] | None = None):
    """Get FastMCP app configured for the detected deployment mode."""
    mode = get_deployment_mode()

    if mode == "exapp":
        from nextcloud_mcp_server.app_exapp import create_exapp_app
        return create_exapp_app(transport, enabled_apps)
    else:
        from nextcloud_mcp_server.app_oauth import create_oauth_app
        return create_oauth_app(transport, enabled_apps)

# Entry point for MCP CLI
mcp = get_app()
```

#### 2. Client Abstraction (auth/client_factory.py)

```python
from typing import Union
from mcp.server.fastmcp import Context

# Conditional import (only available if nc_py_api installed)
try:
    from nc_py_api import NextcloudApp
except ImportError:
    NextcloudApp = None  # type: ignore

async def get_client(ctx: Union[Context, NextcloudApp]) -> NextcloudClient:
    """Get authenticated NextcloudClient for the current request (mode-aware)."""

    # AppAPI mode: ctx is NextcloudApp from nc_py_api
    if NextcloudApp and isinstance(ctx, NextcloudApp):
        return await _get_exapp_client(ctx)

    # OAuth mode: ctx is FastMCP Context
    if isinstance(ctx, Context):
        return await _get_oauth_client(ctx)

    raise TypeError(f"Unexpected context type: {type(ctx)}")

async def _get_exapp_client(nc: NextcloudApp) -> NextcloudClient:
    """Create NextcloudClient from nc_py_api NextcloudApp."""
    # Wrap nc_py_api session in NextcloudClient interface
    return NextcloudClient.from_nc_py_api(nc)

async def _get_oauth_client(ctx: Context) -> NextcloudClient:
    """Create NextcloudClient from OAuth access token."""
    # Existing OAuth logic from context_helper.py
    access_token = ctx.request_context.access_token
    username = access_token.claims.get("preferred_username")
    return NextcloudClient.from_token(
        base_url=settings.nextcloud_host,
        token=access_token.token,
        username=username
    )
```

#### 3. Tool Signatures (server/notes.py)

```python
from typing import Union
from mcp.server.fastmcp import Context

try:
    from nc_py_api import NextcloudApp
except ImportError:
    NextcloudApp = type(None)  # Fallback if not installed

@mcp.tool()
@require_scopes("notes:write")
async def nc_notes_create_note(
    title: str,
    content: str,
    category: str,
    ctx: Union[Context, NextcloudApp],  # Union type for both modes
) -> CreateNoteResponse:
    """Create a new note."""
    client = await get_client(ctx)  # Mode-aware helper
    result = await client.notes.create_note(
        title=title,
        content=content,
        category=category
    )
    return CreateNoteResponse(
        success=True,
        note=result,
        message=f"Created note '{title}'"
    )
```

**Key Points**:
- Tools accept `Context | NextcloudApp` union type
- No changes to business logic or client calls
- `get_client()` abstraction handles mode detection

#### 4. Scope Authorization (auth/scope_authorization.py)

```python
def require_scopes(*required_scopes: str):
    """Decorator for scope/permission checking (mode-aware)."""

    def decorator(func: Callable) -> Callable:
        func._required_scopes = list(required_scopes)

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = kwargs.get("ctx")

            # AppAPI mode: Check Nextcloud permissions
            if NextcloudApp and isinstance(ctx, NextcloudApp):
                await _check_exapp_permissions(ctx, required_scopes)

            # OAuth mode: Check token scopes (existing logic)
            elif isinstance(ctx, Context):
                await _check_oauth_scopes(ctx, required_scopes)

            return await func(*args, **kwargs)

        return wrapper
    return decorator

async def _check_exapp_permissions(nc: NextcloudApp, scopes: list[str]) -> None:
    """Validate ExApp has required permissions via Nextcloud capabilities."""
    # In AppAPI mode, permissions are granted at install time
    # Check against user's Nextcloud capabilities for additional validation
    # E.g., "notes:write" → verify user has notes app enabled
    pass  # Implementation depends on Nextcloud capability API

async def _check_oauth_scopes(ctx: Context, scopes: list[str]) -> None:
    """Validate OAuth token contains required scopes (existing logic)."""
    # ... existing implementation
```

#### 5. Dependency Management (pyproject.toml)

```toml
[project]
dependencies = [
    "mcp[cli] >=1.21,<1.22",
    "httpx >=0.28.1,<0.29.0",
    "pydantic >=2.10.4,<2.11.0",
    # ... other shared dependencies
]

[dependency-groups]
oauth = [
    "authlib >=1.6.5",
    "pyjwt[crypto] >=2.8.0",
    "jwcrypto >=1.5.6",
    "aiosqlite >=0.20.0",
]
exapp = [
    "nc_py_api >=0.21.0",
]
dev = [
    # ... existing dev dependencies
]
```

**Installation**:
```bash
# OAuth mode (current default)
uv sync --group oauth

# AppAPI mode
uv sync --group exapp

# Development (both modes)
uv sync --group oauth --group exapp --group dev
```

#### 6. Docker Multi-Stage Build (Dockerfile)

```dockerfile
# Stage 1: Base image with shared dependencies
FROM python:3.11-slim AS base
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --no-dev

# Stage 2: OAuth variant (default)
FROM base AS oauth
RUN uv sync --group oauth --no-dev
COPY nextcloud_mcp_server/ ./nextcloud_mcp_server/
ENV DEPLOYMENT_MODE=oauth
EXPOSE 8000
CMD ["uv", "run", "mcp", "run", "nextcloud_mcp_server.app:mcp"]

# Stage 3: AppAPI variant
FROM base AS exapp
RUN uv sync --group exapp --no-dev
COPY nextcloud_mcp_server/ ./nextcloud_mcp_server/
COPY appinfo/ ./appinfo/
ENV APPAPI_MODE=true
EXPOSE 8000
CMD ["uv", "run", "python", "-m", "nextcloud_mcp_server.app_exapp"]
```

**Build Commands**:
```bash
# Build OAuth variant
docker build --target oauth -t nextcloud-mcp-server:oauth .

# Build AppAPI variant
docker build --target exapp -t nextcloud-mcp-server:exapp .

# Multi-platform builds
docker buildx build --platform linux/amd64,linux/arm64 \
  --target oauth -t ghcr.io/cbcoutinho/nextcloud-mcp-server:oauth --push .
```

#### 7. ExApp Manifest (appinfo/info.xml)

```xml
<?xml version="1.0"?>
<info xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:noNamespaceSchemaLocation="https://apps.nextcloud.com/schema/apps/info.xsd">
    <id>nextcloud_mcp_server</id>
    <name>Nextcloud MCP Server</name>
    <summary>Model Context Protocol server for AI assistant integration</summary>
    <description><![CDATA[
    Exposes Nextcloud functionality via the Model Context Protocol (MCP).
    Enables AI assistants like Claude to access Notes, Calendar, Contacts, Files, and more.
    Supports semantic search across all Nextcloud content via vector embeddings.
    ]]></description>

    <version>0.33.0</version>
    <licence>agpl</licence>
    <author>Chris Coutinho</author>

    <namespace>NextcloudMcpServer</namespace>
    <category>integration</category>
    <category>tools</category>

    <bugs>https://github.com/cbcoutinho/nextcloud-mcp-server/issues</bugs>
    <repository>https://github.com/cbcoutinho/nextcloud-mcp-server</repository>

    <dependencies>
        <nextcloud min-version="30" max-version="31"/>
    </dependencies>

    <external-app>
        <docker-install>
            <registry>ghcr.io</registry>
            <image>cbcoutinho/nextcloud-mcp-server</image>
            <image-tag>exapp</image-tag>
        </docker-install>

        <scopes>
            <value>FILES</value>
            <value>NOTIFICATIONS</value>
            <value>CALENDAR</value>
            <value>CONTACTS</value>
            <value>ALL</value>
        </scopes>

        <routes>
            <route>
                <url>/mcp.*</url>
                <verb>GET,POST</verb>
                <access_level>USER</access_level>
                <headers_to_exclude>[]</headers_to_exclude>
            </route>
        </routes>
    </external-app>
</info>
```

**Proxy Route**: `/index.php/apps/app_api/proxy/nextcloud_mcp_server/mcp/sse`

### Configuration

#### OAuth Mode (Existing)
```bash
# Environment variables (unchanged)
NEXTCLOUD_HOST=https://nextcloud.example.com
OIDC_CLIENT_ID=abc123
OIDC_CLIENT_SECRET=xyz789
ENABLE_TOKEN_EXCHANGE=false
```

#### AppAPI Mode (New)
```bash
# Environment variables
APPAPI_MODE=true
APP_ID=nextcloud_mcp_server
APP_SECRET=<shared-secret-from-appapi>
APP_VERSION=0.33.0
NEXTCLOUD_URL=https://nextcloud.example.com
```

### MCP Client Configuration

#### OAuth Mode
```json
{
  "mcpServers": {
    "nextcloud": {
      "url": "https://mcp.example.com/sse",
      "transport": "sse"
    }
  }
}
```

#### AppAPI Mode (via Nextcloud Proxy)
```json
{
  "mcpServers": {
    "nextcloud": {
      "url": "https://nextcloud.example.com/index.php/apps/app_api/proxy/nextcloud_mcp_server/mcp/sse",
      "transport": "sse",
      "headers": {
        "Cookie": "nc_session=<user-session-cookie>"
      }
    }
  }
}
```

## Consequences

### Positive

1. **Flexible Deployment**: Users choose the mode that fits their use case—OAuth for multi-tenant SaaS, AppAPI for single-tenant integration.

2. **Simplified Administration**: AppAPI mode enables one-click installation via Nextcloud app store, dramatically reducing setup complexity for single-tenant deployments.

3. **Native Nextcloud Integration**: AppAPI mode unlocks UI components (menu items, file actions), event listeners, and dashboard widgets.

4. **No Breaking Changes**: OAuth mode remains the default; all existing deployments continue working without modification.

5. **Maximum Code Sharing**: 90%+ code reuse achieved through abstraction layer—tools, clients, models, and business logic fully shared.

6. **Future-Proof**: As Nextcloud's ExApp ecosystem matures, we gain access to new integration capabilities without architectural changes.

7. **Ecosystem Compatibility**: AppAPI mode makes the MCP server compatible with other Nextcloud ExApps, enabling potential cross-app integrations.

### Negative

1. **Increased Complexity**: Abstraction layer adds indirection and complexity to the codebase. Developers must understand both authentication mechanisms.

2. **Testing Burden**: Must test both modes in CI/CD. Integration test matrix doubles in size.

3. **Dependency Management**: Optional dependencies (`authlib` vs `nc_py_api`) complicate installation and development setup.

4. **Documentation Overhead**: Users need clear guidance on when to use each mode, how they differ in behavior, and how to configure them.

5. **Maintenance Burden**: Two authentication paths to maintain, two deployment modes to support, two sets of environment variables to document.

6. **Feature Parity Challenges**: Some features may work better in one mode than the other (e.g., background jobs in OAuth mode, UI integration in AppAPI mode).

### Neutral

1. **Separate Docker Images**: Users explicitly choose via image tag (`:oauth` or `:exapp`), making mode selection clear but requiring separate builds.

2. **Mode-Specific Features**: Some capabilities only work in specific modes (progressive consent in OAuth, UI registration in AppAPI).

3. **Community Bifurcation**: User base may split between OAuth and AppAPI camps, potentially fragmenting community support.

4. **AppAPI Dependency**: AppAPI mode's stability depends on Nextcloud's AppAPI maintenance and evolution.

## Alternative Approaches Considered

### Alternative 1: OAuth-Only (Status Quo)

**Description**: Continue with current OAuth architecture, do not add AppAPI support.

**Pros**:
- No additional complexity
- Single authentication mechanism to maintain
- Works today with multi-tenant support

**Cons**:
- Misses opportunity for simplified Nextcloud integration
- Higher barrier to entry for single-tenant users
- Cannot leverage ExApp ecosystem benefits
- Requires separate deployment and management infrastructure

**Rejected Because**: Ignores clear demand for native Nextcloud integration and simpler deployment model.

### Alternative 2: Migrate Fully to AppAPI

**Description**: Remove OAuth support entirely, implement only AppAPI authentication.

**Pros**:
- Simpler codebase (single authentication mechanism)
- Tight Nextcloud integration
- Easier administration for Nextcloud users

**Cons**:
- **Breaking change**: All existing OAuth deployments stop working
- **Loses multi-tenant capability**: Cannot serve multiple Nextcloud instances from one MCP server
- **External client support lost**: MCP clients not integrated with Nextcloud cannot connect
- **Background job complexity**: Would need to reimplement background sync using Nextcloud cron

**Rejected Because**: Breaks existing deployments and eliminates valuable multi-tenant capabilities.

### Alternative 3: Separate Repositories

**Description**: Fork codebase into `nextcloud-mcp-server` (OAuth) and `nextcloud-mcp-exapp` (AppAPI).

**Pros**:
- Clean separation of concerns
- No abstraction layer complexity
- Independent versioning and release cycles

**Cons**:
- **Massive code duplication**: 90%+ of code (tools, clients, models) duplicated
- **Maintenance nightmare**: Bug fixes must be applied to both repos
- **Community fragmentation**: Two projects instead of one unified effort
- **Feature divergence**: Repositories inevitably drift apart over time

**Rejected Because**: Maintenance burden far outweighs benefits of clean separation.

### Alternative 4: AppAPI Plugin to Existing OAuth Server

**Description**: Keep OAuth server unchanged, create separate AppAPI plugin that proxies to it.

**Pros**:
- No changes to existing OAuth server
- AppAPI support added as separate component

**Cons**:
- **Double proxying**: Client → Nextcloud → AppAPI plugin → OAuth MCP server → Nextcloud
- **Authentication complexity**: AppAPI plugin must translate headers to OAuth tokens
- **Performance overhead**: Extra network hops add latency
- **Deployment complexity**: Two containers to manage instead of one

**Rejected Because**: Architectural complexity and performance overhead unjustified.

### Alternative 5: Runtime Mode Switching (Single Image)

**Description**: Single Docker image that detects mode at runtime via environment variable (no separate builds).

**Pros**:
- Single image to build and distribute
- Simplest CI/CD pipeline
- Users can switch modes by changing environment variables

**Cons**:
- **Larger image size**: Includes both `authlib` and `nc_py_api` dependencies
- **Unused code**: OAuth-only deployments carry AppAPI code and vice versa
- **Security surface**: All dependencies present even if not used

**Decision**: Implement multi-stage Docker builds instead for smaller, mode-specific images.

## Implementation Plan

### Phase 1: Foundation (Days 1-3)

**Goal**: Create abstraction layer without breaking existing OAuth functionality.

1. Extract OAuth setup from `app.py` into `app_oauth.py`
2. Add mode detection function to `app.py`
3. Create `auth/client_factory.py` with `get_client()` abstraction
4. Update `context.py` to accept `Context | NextcloudApp` union type
5. Make `@require_scopes` decorator mode-aware
6. Add conditional imports for `nc_py_api` (graceful degradation if not installed)
7. Run full test suite—all existing OAuth tests must pass

### Phase 2: AppAPI Implementation (Days 4-6)

**Goal**: Implement AppAPI mode and achieve feature parity with OAuth.

1. Create `app_exapp.py` with FastAPI + `AppAPIAuthMiddleware` setup
2. Implement `auth/appapi_auth.py` for header validation
3. Add `NextcloudClient.from_nc_py_api()` adapter method
4. Create `appinfo/info.xml` ExApp manifest
5. Configure proxy routes for `/mcp/*` endpoints
6. Implement ExApp-specific permission checking in `scope_authorization.py`
7. Manual testing: Register ExApp in test Nextcloud instance, verify connection

### Phase 3: Build & Deployment (Days 7-8)

**Goal**: Prepare distribution artifacts for both modes.

1. Update `Dockerfile` with multi-stage builds (base → oauth, base → exapp)
2. Add dependency groups to `pyproject.toml` (`oauth`, `exapp`)
3. Update CI/CD pipeline to build both Docker images (`:oauth`, `:exapp`)
4. Create AppAPI deployment documentation (`docs/appapi-deployment.md`)
5. Update `docs/installation.md` with deployment options section
6. Update `docs/authentication.md` with AppAPI mode details
7. Test Docker builds locally for both modes

### Phase 4: Testing & Documentation (Days 9-10)

**Goal**: Ensure both modes work correctly and are well-documented.

1. Update `tests/conftest.py` to support mode-based fixtures
2. Add `TEST_MODE` environment variable for test execution
3. Run integration tests against both modes
4. Add AppAPI-specific integration tests (proxy, header validation)
5. Update `README.md` with deployment options comparison table
6. Create this ADR (ADR-011)
7. Write blog post / announcement explaining hybrid architecture

**Total Estimated Time**: 10 days

### Success Criteria

- ✅ All existing OAuth tests pass without modification
- ✅ New AppAPI integration tests pass
- ✅ Both Docker images build successfully
- ✅ ExApp can be registered in test Nextcloud instance
- ✅ MCP client can connect via AppAPI proxy
- ✅ All MCP tools work in both modes
- ✅ No breaking changes for existing OAuth deployments
- ✅ Documentation covers both deployment modes

## Related Documentation

### To Update
- `docs/installation.md` - Add AppAPI installation section
- `docs/authentication.md` - Document both authentication modes
- `docs/configuration.md` - Add AppAPI environment variables
- `README.md` - Add deployment options comparison

### To Create
- `docs/appapi-deployment.md` - Comprehensive AppAPI setup guide
- `docs/architecture-hybrid.md` - Detailed hybrid architecture documentation
- `docs/comparison-oauth-vs-appapi.md` - When to use each mode

### Related ADRs
- **ADR-004**: Progressive Consent (OAuth-specific, note AppAPI differences)
- **ADR-005**: Token Audience Validation (OAuth-specific)
- **ADR-007**: Background Vector Sync (note implications for AppAPI mode)

## Open Questions

### Resolved via Research (2025-01-13)

The following questions have been investigated and resolved through detailed analysis of AppAPI and nc_py_api codebases:

**1. Non-Browser Client Authentication** ✅ **Resolved**

**Question**: How do MCP clients (Claude Desktop, CLI tools) authenticate to the AppAPI proxy when they can't handle browser-based session cookies?

**Answer**: AppAPI proxy currently relies on Nextcloud session cookies. Non-browser clients require one of:
- **App Passwords** (Recommended Phase 1): Add BasicAuth support to AppAPI proxy. Users generate app passwords in Nextcloud UI, MCP clients send `Authorization: Basic` headers. Requires upstream contribution to AppAPI.
- **OAuth 2.0** (Long-term): Integrate MCP OAuth with Nextcloud OIDC provider, validate Bearer tokens via introspection.

**Decision**: Document app password requirement. Contribute BasicAuth support to AppAPI upstream. See "Authentication Challenges" section for implementation details.

**2. Streaming Transport Compatibility** ✅ **Resolved**

**Question**: How does the AppAPI proxy handle streamable HTTP? Will it work when MCP deprecates SSE?

**Answer**: AppAPI proxy supports **buffered streaming** via Guzzle's `stream: true`, not real-time streaming:
- ExApp generates response chunks
- Proxy accumulates complete response
- Client receives full stream after completion
- Works for bounded operations (finite event streams)
- **Does NOT support**: Real-time incremental streaming, WebSocket protocol

**Transport Compatibility**:
- ✅ **Streamable HTTP** (MCP 2025-03-26+): Works today, buffered
- ❌ **WebSocket**: Would require major proxy rewrite (not viable)
- ⚠️ **stdio**: Ideal for containerized ExApps but requires AppAPI changes (`docker exec -i` support)

**Decision**: Use Streamable HTTP transport for AppAPI mode. Document buffering limitations. Advocate for stdio transport support in future AppAPI versions. See "Streaming Limitations" section for details.

**3. Callbacks and Progress Updates** ✅ **Resolved**

**Question**: How do callbacks/webhooks work with ExApps? Can MCP's notification model work through the proxy?

**Answer**: **Fundamental incompatibility** between MCP's notification model and AppAPI's request/response architecture:

**What DOES work**:
- ✅ Webhooks (Nextcloud → ExApp): Well supported via `webhooks_listener`
- ✅ Notifications (ExApp → Nextcloud): Via `nc.notifications.create()`
- ✅ Background tasks: Via FastAPI `BackgroundTasks`

**What DOESN'T work**:
- ❌ MCP protocol notifications (server → client): No persistent connection through proxy
- ❌ Real-time progress updates: No channel to original MCP client
- ❌ Sampling/RAG features: Requires bidirectional MCP protocol

**Decision**:
- Short operations (<30s): Direct request/response (works fine)
- Long operations: Use `BackgroundTasks` + Nextcloud notifications
- Sampling features: **OAuth mode only** (not possible in AppAPI)
- Document limitations clearly in Feature Parity Matrix

See "Notification and Progress Update Limitations" section for workarounds.

### Remaining Open Questions

**4. Scope-to-Permission Mapping**: How should MCP scopes (e.g., `notes:write`) map to Nextcloud capability checks? Need Nextcloud capability API documentation to implement robust permission validation in AppAPI mode.

**5. Background Jobs Architecture**: OAuth mode uses background workers with refresh tokens. AppAPI mode should likely use:
   - Webhook-driven processing (ADR-010 pattern)
   - Nextcloud cron jobs for periodic tasks
   - Different sync strategy than OAuth mode

**6. UI Integration Priority**: Should initial AppAPI implementation include UI components (menu items, file actions), or ship with proxy support only and add UI later? Recommend: Phase 1 without UI, add in Phase 2 if needed.

**7. Version Compatibility**: What range of Nextcloud versions should we support? `info.xml` currently specifies 30-31. Should we support older versions (28-29)?

These remaining questions should be resolved during implementation. Create follow-up issues to track decisions.

## Conclusion

After comprehensive research and analysis, **the hybrid OAuth + AppAPI architecture is not viable** for this project's use case. While AppAPI ExApps provide value for in-app Nextcloud integration, the architectural constraints fundamentally conflict with MCP's protocol requirements for external client integration.

### Key Findings

1. **Architectural Incompatibility is Fundamental**
   - MCP requires multi-turn nested interactions (sampling, elicitation)
   - AppAPI provides stateless request/response proxy only
   - No amount of implementation effort can bridge this gap
   - Would require complete AppAPI proxy redesign (WebSocket support, message routing, stateful connections)

2. **Validation from Nextcloud's Own Projects**
   - Context Agent (official Nextcloud ExApp with MCP) faces identical limitations
   - Works around them by using Task Processing API instead of MCP protocol
   - Proves limitations are inherent to ExApp architecture, not our implementation
   - Even official projects cannot make MCP's bidirectional features work through AppAPI proxy

3. **Critical Features Lost in AppAPI Mode**
   - ❌ Sampling/RAG (ADR-008) - Core semantic search value proposition
   - ❌ Real-time progress updates - Essential for long-running operations
   - ❌ Bidirectional streaming - Foundation of MCP protocol
   - ⚠️ Buffered-only streaming - Defeats purpose of streaming

4. **Different Use Cases Require Different Solutions**
   - **External MCP clients** (our use case): Require OAuth mode
   - **In-app integration** (Context Agent's use case): Use Task Processing API
   - These are fundamentally different products with different capabilities

### Decision

**Continue with OAuth mode exclusively.** It provides all essential features:
- ✅ Full MCP protocol support (sampling, elicitation, streaming)
- ✅ Multi-tenant capability
- ✅ External client integration (Claude Desktop, custom clients)
- ✅ Fine-grained per-user permissions
- ✅ Real-time progress updates
- ✅ Production-ready architecture

### Future Consideration

If in-app Nextcloud integration is desired later, the correct approach would be:
- Register as **Task Processing provider** (separate from MCP server)
- Integrate with **Nextcloud Assistant UI**
- Use **Task Processing API** for orchestration
- Accept this is a **different product** with different capabilities (no sampling, custom confirmation flows)

This ADR documents the research and analysis that led to this decision, preserving the investigation for future reference and demonstrating due diligence in exploring AppAPI integration.
