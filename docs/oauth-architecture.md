# OAuth Architecture

This document explains how OAuth2/OIDC authentication works in the Nextcloud MCP Server implementation.

## Overview

The Nextcloud MCP Server acts as an **OAuth 2.0 Resource Server**, protecting access to Nextcloud resources. It relies on Nextcloud's OIDC Identity Provider for user authentication and token validation.

## Architecture Diagram

The complete OAuth flow includes server startup (with DCR), client discovery (with PRM), authorization (with PKCE), and API access phases:

```
═══════════════════════════════════════════════════════════════════════════════════
Phase 0: MCP Server Startup & Client Registration (DCR - RFC 7591)
═══════════════════════════════════════════════════════════════════════════════════

                                 ┌──────────────────┐                  ┌─────────────────┐
                                 │   MCP Server     │                  │   Nextcloud     │
                                 │   (Resource      │                  │  (OIDC Provider)│
                                 │    Server)       │                  │                 │
                                 └────────┬─────────┘                  └────────┬────────┘
                                          │                                     │
                                          │  0a. OIDC Discovery                 │
                                          ├────────────────────────────────────>│
                                          │  GET                                │
                                          |  /.well-known/openid-configuration  │
                                          │                                     │
                                          │  0b. Discovery response             │
                                          │<────────────────────────────────────┤
                                          │  {issuer, endpoints, PKCE methods}  │
                                          │                                     │
                                          │  0c. Register OAuth client (DCR)    │
                                          ├────────────────────────────────────>│
                                          │  POST /apps/oidc/register           │
                                          │  {client_name, redirect_uris,       │
                                          │   scopes, token_type}               │
                                          │                                     │
                                          │  0d. Client credentials             │
                                          │<────────────────────────────────────┤
                                          │  {client_id, client_secret}         │
                                          │  → Saved to .nextcloud_oauth_*.json │
                                          │                                     │
                                          │  ✓ Server ready for connections     │


═══════════════════════════════════════════════════════════════════════════════════
Phase 1: Client Connection & Discovery (PRM - RFC 9728)
═══════════════════════════════════════════════════════════════════════════════════

┌─────────────┐                  ┌──────────────────┐                  ┌─────────────────┐
│             │                  │   MCP Server     │                  │   Nextcloud     │
│ MCP Client  │                  │   (Resource      │                  │   Instance      │
│ (Claude)    │                  │    Server)       │                  │                 │
│             │                  │                  │                  │                 │
└──────┬──────┘                  └────────┬─────────┘                  └────────┬────────┘
       │                                  │                                     │
       │  1a. Connect to MCP              │                                     │
       ├─────────────────────────────────>│                                     │
       │                                  │                                     │
       │  1b. Return auth settings        │                                     │
       │<─────────────────────────────────┤                                     │
       │  {issuer_url, resource_url}      │                                     │
       │                                  │                                     │
       │  1c. PRM Discovery (RFC 9728)    │                                     │
       ├─────────────────────────────────>│                                     │
       │  GET /.well-known/oauth-         │                                     │
       │      protected-resource/mcp      │                                     │
       │                                  │                                     │
       │  1d. PRM response (scopes!)      │                                     │
       │<─────────────────────────────────┤                                     │
       │  {resource, scopes_supported,    │  ← Dynamically discovered from      │
       │   authorization_servers}         │    @require_scopes decorators       │
       │                                  │                                     │


═══════════════════════════════════════════════════════════════════════════════════
Phase 2: OAuth Authorization Flow (PKCE - RFC 7636)
═══════════════════════════════════════════════════════════════════════════════════

       │                                  │                                     │
       │  2a. Generate PKCE challenge     │                                     │
       │  code_verifier = random(43-128)  │                                     │
       │  code_challenge = SHA256(verif.) │                                     │
       │                                  │                                     │
       │  2b. Authorization request       │                                     │
       ├──────────────────────────────────┼────────────────────────────────────>│
       │  /apps/oidc/authorize?           │                                     │
       │    client_id=xxx                 │                                     │
       │    &code_challenge=abc...        │                                     │
       │    &code_challenge_method=S256   │                                     │
       │    &scope=openid notes:read ...  │                                     │
       │                                  │                                     │
       │  2c. User consent page           │                                     │
       │<─────────────────────────────────┼─────────────────────────────────────┤
       │  (Browser: Select scopes)        │                                     │
       │                                  │                                     │
       │  2d. User grants scopes          │                                     │
       ├──────────────────────────────────┼────────────────────────────────────>│
       │                                  │                                     │
       │  2e. Authorization code redirect │                                     │
       │<─────────────────────────────────┼─────────────────────────────────────┤
       │  callback?code=xyz123            │                                     │
       │                                  │                                     │
       │  2f. Exchange code for token     │                                     │
       ├──────────────────────────────────┼────────────────────────────────────>│
       │  POST /apps/oidc/token           │                                     │
       │  {code, code_verifier,           │  ← Validates PKCE challenge         │
       │   client_id, client_secret}      │                                     │
       │                                  │                                     │
       │  2g. Access token (JWT/opaque)   │                                     │
       │<─────────────────────────────────┼─────────────────────────────────────┤
       │  {access_token, token_type,      │                                     │
       │   scope: "openid notes:read...") │  ← User's granted scopes            │
       │                                  │                                     │


═══════════════════════════════════════════════════════════════════════════════════
Phase 3: MCP Tool Access (Scope-based Authorization)
═══════════════════════════════════════════════════════════════════════════════════

       │                                  │                                     │
       │  3a. list_tools request          │                                     │
       ├─────────────────────────────────>│                                     │
       │  Authorization: Bearer <token>   │                                     │
       │                                  │                                     │
       │                                  │  3b. Validate token                 │
       │                                  ├────────────────────────────────────>│
       │                                  │  GET /apps/oidc/userinfo            │
       │                                  │  Authorization: Bearer <token>      │
       │                                  │                                     │
       │                                  │  3c. Token valid + scopes           │
       │                                  │<────────────────────────────────────┤
       │                                  │  {sub, scopes, ...}                 │
       │                                  │  ← Cached for 1 hour                │
       │                                  │                                     │
       │  3d. Filtered tool list          │                                     │
       │<─────────────────────────────────┤  ← Only tools matching user's       │
       │  [tools matching token scopes]   │    token scopes (via @require_scopes)
       │                                  │                                     │
       │  3e. Call tool                   │                                     │
       ├─────────────────────────────────>│                                     │
       │  nc_notes_get_note(note_id=1)    │  ← @require_scopes("notes:read")    │
       │  Authorization: Bearer <token>   │                                     │
       │                                  │                                     │
       │                                  │  3f. Scope check PASSED             │
       │                                  │  ✓ Token has notes:read             │
       │                                  │                                     │
       │                                  │  3g. Nextcloud API call             │
       │                                  ├────────────────────────────────────>│
       │                                  │  GET /apps/notes/api/v1/notes/1     │
       │                                  │  Authorization: Bearer <token>      │
       │                                  │  ← user_oidc validates Bearer token │
       │                                  │                                     │
       │                                  │  3h. API response                   │
       │                                  │<────────────────────────────────────┤
       │                                  │  {id: 1, title: "Note", ...}        │
       │                                  │                                     │
       │  3i. MCP tool response           │                                     │
       │<─────────────────────────────────┤                                     │
       │  {note data}                     │                                     │
       │                                  │                                     │


═══════════════════════════════════════════════════════════════════════════════════
Insufficient Scope Example (Step-Up Authorization)
═══════════════════════════════════════════════════════════════════════════════════

       │  4a. Call write tool             │                                     │
       ├─────────────────────────────────>│                                     │
       │  nc_notes_create_note(...)       │  ← @require_scopes("notes:write")   │
       │  Authorization: Bearer <token>   │                                     │
       │                                  │                                     │
       │                                  │  4b. Scope check FAILED             │
       │                                  │  ✗ Token only has notes:read        │
       │                                  │                                     │
       │  4c. 403 Insufficient Scope      │                                     │
       │<─────────────────────────────────┤                                     │
       │  WWW-Authenticate: Bearer        │                                     │
       │    error="insufficient_scope",   │                                     │
       │    scope="notes:write",          │                                     │
       │    resource_metadata="..."       │                                     │
       │                                  │                                     │
       │  → Client can re-authorize with  │                                     │
       │    additional scopes (Step-Up)   │                                     │
       │                                  │                                     │
```

## Components

### 1. MCP Client (e.g., Claude Desktop, Claude Code)

**Capabilities**:
- Discovers OAuth configuration via MCP server
- Queries PRM endpoint for supported scopes
- Initiates OAuth flow with PKCE (Proof Key for Code Exchange)
- Stores and sends access token with each request
- Handles scope-based tool filtering
- Supports step-up authorization (re-auth for additional scopes)

**Examples**: Claude Desktop, Claude Code, MCP Inspector, custom MCP clients

### 2. MCP Server (Resource Server - This Implementation)

**Role**: OAuth 2.0 Resource Server (RFC 6749)

**Responsibilities**:

#### Startup Phase
- **OIDC Discovery**: Queries `/.well-known/openid-configuration` for OAuth endpoints
- **PKCE Validation**: Verifies server advertises S256 code challenge method
- **Dynamic Client Registration (DCR)**: Automatically registers OAuth client via `/apps/oidc/register` (RFC 7591)
  - Or loads pre-configured client credentials
  - Saves credentials to `.nextcloud_oauth_client.json`
- **Tool Registration**: Loads all MCP tools with their `@require_scopes` decorators

#### Client Connection Phase
- **Auth Settings**: Returns OAuth issuer URL and resource URL
- **PRM Endpoint**: Exposes `/.well-known/oauth-protected-resource/mcp` (RFC 9728)
  - Dynamically discovers scopes from all registered tools
  - Returns `scopes_supported` list based on `@require_scopes` decorators

#### Request Processing Phase
- **Token Validation**: Validates Bearer tokens via Nextcloud userinfo endpoint
  - Supports both JWT and opaque tokens
  - Caches validation results (1-hour TTL)
  - Extracts user identity and granted scopes
- **Scope Enforcement**:
  - Filters `list_tools` based on user's token scopes
  - Validates scopes before executing each tool
  - Returns 403 with `WWW-Authenticate` header for insufficient scopes
- **Per-User Clients**: Creates authenticated `NextcloudClient` instance per user
  - Uses Bearer token for all Nextcloud API requests
  - User-specific permissions and audit trails

**Key Files**:
- [`app.py`](../nextcloud_mcp_server/app.py) - OAuth mode, DCR, PRM endpoint
- [`auth/token_verifier.py`](../nextcloud_mcp_server/auth/token_verifier.py) - Token validation (userinfo + introspection + JWT)
- [`auth/context_helper.py`](../nextcloud_mcp_server/auth/context_helper.py) - Per-user client creation
- [`auth/scope_authorization.py`](../nextcloud_mcp_server/auth/scope_authorization.py) - `@require_scopes` decorator, scope discovery
- [`auth/client_registration.py`](../nextcloud_mcp_server/auth/client_registration.py) - DCR implementation (RFC 7591)

### 3. Nextcloud OIDC Apps

#### a) `oidc` - OIDC Identity Provider

**Role**: OAuth 2.0 Authorization Server + OIDC Provider

**Location**: Nextcloud app (`apps/oidc`)

**Endpoints**:
- `/.well-known/openid-configuration` - OIDC Discovery (RFC 8414)
- `/apps/oidc/authorize` - Authorization endpoint (OAuth 2.0 + PKCE)
- `/apps/oidc/token` - Token endpoint (issues JWT or opaque tokens)
- `/apps/oidc/userinfo` - UserInfo endpoint (OIDC Core, used for token validation)
- `/apps/oidc/jwks` - JSON Web Key Set (for JWT signature verification)
- `/apps/oidc/register` - Dynamic Client Registration endpoint (RFC 7591)
- `/apps/oidc/introspect` - Token Introspection endpoint (RFC 7662, optional)

**Token Types**:
- **JWT tokens**: Self-contained tokens with embedded scopes, validated via JWKS or userinfo
- **Opaque tokens**: Random strings, validated via userinfo or introspection endpoint

**Configuration**:
```bash
# Enable dynamic client registration (recommended for development)
# Nextcloud Admin → Settings → OIDC → "Allow dynamic client registration"

# Enable token introspection (optional, for opaque token validation)
# Nextcloud Admin → Settings → OIDC → "Enable token introspection"
```

#### b) `user_oidc` - OpenID Connect User Backend

**Role**: Bearer token validation middleware for Nextcloud APIs

**Location**: Nextcloud app (`apps/user_oidc`)

**Responsibilities**:
- Intercepts Nextcloud API requests with `Authorization: Bearer` header
- Validates tokens against OIDC provider (`oidc` app)
- Creates authenticated user sessions
- Enforces user-specific permissions on API requests

**Configuration**:
```bash
# Enable Bearer token validation (required for OAuth mode)
php occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean
```

> [!IMPORTANT]
> The `user_oidc` app requires a patch to properly support Bearer token authentication for non-OCS endpoints (like Notes API, Calendar API). See [Upstream Status](oauth-upstream-status.md) for patch details and PR status.

### 4. Nextcloud Instance

**Role**: Resource Owner + API Provider

**APIs Exposed**:
- **Notes API**: `/apps/notes/api/v1/` - Note CRUD operations
- **Calendar (CalDAV)**: `/remote.php/dav/calendars/` - Events and todos
- **Contacts (CardDAV)**: `/remote.php/dav/addressbooks/` - Contact management
- **Cookbook API**: `/apps/cookbook/api/v1/` - Recipe management
- **Deck API**: `/apps/deck/api/v1.0/` - Kanban boards
- **Tables API**: `/apps/tables/api/2/` - Table row operations
- **WebDAV (Files)**: `/remote.php/dav/files/` - File operations
- **Sharing API**: `/ocs/v2.php/apps/files_sharing/api/v1/` - Share management

## Authentication Flow

The OAuth flow consists of four distinct phases (see diagram above for visual representation):

### Phase 0: MCP Server Startup (One-time Setup)

**Happens**: On MCP server first startup

**Steps**:
1. **OIDC Discovery** (`GET /.well-known/openid-configuration`)
   - MCP server queries Nextcloud for OAuth endpoints
   - Validates PKCE support (requires `S256` code challenge method)
   - Extracts endpoints: authorize, token, userinfo, jwks, register

2. **Dynamic Client Registration** (`POST /apps/oidc/register`)
   - If no pre-configured client credentials exist
   - MCP server registers itself as OAuth client (RFC 7591)
   - Provides: client name, redirect URIs, requested scopes, token type
   - Receives: `client_id`, `client_secret`
   - Saves credentials to `.nextcloud_oauth_client.json`

3. **Tool Registration**
   - All MCP tools loaded with their `@require_scopes` decorators
   - Scope metadata stored for later discovery

**Result**: MCP server ready to accept client connections

### Phase 1: Client Discovery (Per MCP Client Connection)

**Happens**: When MCP client first connects

**Steps**:
1. **MCP Connection**
   - Client connects to MCP server
   - Server returns OAuth auth settings (issuer URL, resource URL)

2. **PRM Discovery** (`GET /.well-known/oauth-protected-resource/mcp`)
   - Client queries Protected Resource Metadata endpoint (RFC 9728)
   - Server **dynamically discovers** scopes from all registered tools
   - Returns: resource URL, `scopes_supported` list, authorization servers
   - Client now knows which scopes are available

**Result**: Client knows OAuth configuration and available scopes

### Phase 2: OAuth Authorization (PKCE Flow - RFC 7636)

**Happens**: User authorizes access

**Steps**:
1. **PKCE Challenge Generation** (Client-side)
   - Generate `code_verifier`: random 43-128 character string
   - Calculate `code_challenge`: `BASE64URL(SHA256(code_verifier))`

2. **Authorization Request** (`GET /apps/oidc/authorize`)
   - Client redirects user to Nextcloud consent page
   - Parameters:
     - `client_id`: OAuth client ID
     - `code_challenge`: SHA256 hash of verifier
     - `code_challenge_method`: `S256`
     - `scope`: Requested scopes (e.g., `openid notes:read notes:write`)
     - `redirect_uri`: MCP server callback URL

3. **User Consent**
   - User authenticates to Nextcloud (if not already logged in)
   - User reviews and approves/denies requested scopes
   - Can select subset of requested scopes

4. **Authorization Code**
   - Nextcloud redirects to `callback?code=xyz123`
   - Code is bound to PKCE challenge

5. **Token Exchange** (`POST /apps/oidc/token`)
   - Client sends:
     - Authorization `code`
     - `code_verifier` (proves possession of original challenge)
     - `client_id` and `client_secret`
   - Nextcloud validates PKCE challenge: `SHA256(code_verifier) == code_challenge`
   - Nextcloud issues access token

6. **Access Token Response**
   - Token type: JWT or opaque (configurable)
   - Contains user's **granted scopes** (may be subset of requested)
   - Client stores token for subsequent requests

**Result**: Client has valid access token with granted scopes

### Phase 3: MCP Tool Access (Scope-Based Authorization)

**Happens**: Every MCP tool invocation

**Steps**:

#### Tool Listing (`list_tools`)
1. **List Tools Request**
   - Client sends `list_tools` with `Authorization: Bearer <token>`

2. **Token Validation**
   - MCP server calls `/apps/oidc/userinfo` with Bearer token
   - Nextcloud returns user info including **granted scopes**
   - Result cached for 1 hour

3. **Dynamic Tool Filtering**
   - Server compares token scopes with each tool's `@require_scopes`
   - Only returns tools where user has all required scopes
   - Example: Token with `notes:read` sees 4 read tools, not 3 write tools

4. **Filtered Tool List**
   - Client receives only tools they can use

#### Tool Execution (e.g., `nc_notes_get_note`)
1. **Tool Call**
   - Client invokes tool with `Authorization: Bearer <token>`

2. **Scope Validation**
   - `@require_scopes` decorator extracts token scopes
   - Verifies token contains required scope (e.g., `notes:read`)
   - If missing → 403 with `WWW-Authenticate` header (step-up auth)
   - If present → continues execution

3. **Nextcloud API Call**
   - MCP server creates `NextcloudClient` with Bearer token
   - Calls Nextcloud API (e.g., `GET /apps/notes/api/v1/notes/1`)
   - `user_oidc` app validates Bearer token again
   - Request executes as authenticated user

4. **Response**
   - Nextcloud returns data
   - MCP server formats response
   - Returns to client

**Result**: User can only access tools and data they have permissions for

### Phase 4: Insufficient Scope Handling (Step-Up Authorization)

**Happens**: When user lacks required scopes

**Steps**:
1. **Tool Call with Insufficient Scopes**
   - User calls `nc_notes_create_note` (requires `notes:write`)
   - But token only has `notes:read`

2. **Scope Validation Fails**
   - `@require_scopes("notes:write")` decorator checks token
   - Finds `notes:write` missing

3. **403 Response with Challenge**
   - Returns `403 Forbidden`
   - Includes `WWW-Authenticate` header:
     ```
     Bearer error="insufficient_scope",
            scope="notes:write",
            resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"
     ```

4. **Client Re-Authorization** (Optional)
   - Client can initiate new OAuth flow requesting additional scopes
   - User re-consents with expanded permissions
   - New token includes both `notes:read` and `notes:write`

**Result**: User can dynamically upgrade permissions without full re-authentication

## Token Validation

The MCP server validates tokens using the **userinfo endpoint approach**:

### Why Userinfo (vs JWT Validation)?

**Advantages**:
- Works with both JWT and opaque tokens
- No need to manage JWKS rotation
- Always up-to-date (respects token revocation)
- Simpler implementation

**Caching Strategy**:
- Validated tokens cached for 1 hour (configurable)
- Cache keyed by token string
- Expired tokens re-validated automatically

**Implementation**: See [`NextcloudTokenVerifier`](../nextcloud_mcp_server/auth/token_verifier.py)

## PKCE Requirement

The MCP server **requires** PKCE with S256 code challenge method:

1. Server validates OIDC discovery advertises PKCE support
2. Checks for `code_challenge_methods_supported` field
3. Verifies `S256` is included in supported methods
4. Logs error if PKCE not properly advertised

**Why PKCE?**:
- Required by MCP specification
- Protects against authorization code interception
- Essential for public clients (desktop apps, CLI tools)

**Implementation**: See [`validate_pkce_support()`](../nextcloud_mcp_server/app.py#L31-L93)

## Client Registration

The MCP server supports two client registration modes:

### Automatic Registration (Dynamic Client Registration)

```bash
# No client credentials needed
NEXTCLOUD_HOST=https://nextcloud.example.com
```

**How it works**:
1. Server checks `/.well-known/openid-configuration` for `registration_endpoint`
2. Calls `/apps/oidc/register` to register a client on first startup
3. Saves credentials to `.nextcloud_oauth_client.json`
4. Reuses these credentials on subsequent startups
5. Re-registers only if credentials are missing or expired

**Best for**: Development, testing, quick deployments

### Pre-configured Client

```bash
# Manual client registration via CLI
php occ oidc:create --name="MCP Server" --type=confidential --redirect-uri="http://localhost:8000/oauth/callback"

# Configure MCP server
NEXTCLOUD_HOST=https://nextcloud.example.com
NEXTCLOUD_OIDC_CLIENT_ID=abc123
NEXTCLOUD_OIDC_CLIENT_SECRET=xyz789
```

**Best for**: Production, long-running deployments

## Per-User Client Instances

Each authenticated user gets their own `NextcloudClient` instance:

```python
# From MCP context (contains validated token)
client = get_client_from_context(ctx)

# Creates NextcloudClient with:
# - username: from token's 'sub' or 'preferred_username' claim
# - auth: BearerAuth(token)
```

**Benefits**:
- User-specific permissions
- Audit trail (actions appear from correct user)
- No shared credentials
- Multi-user support

**Implementation**: See [`get_client_from_context()`](../nextcloud_mcp_server/auth/context_helper.py)

## Security Considerations

### Token Storage
- MCP client stores access token
- MCP server does NOT store tokens (validates per-request)
- Token validation results cached in-memory only

### PKCE Protection
- Server validates PKCE is advertised
- Client MUST use PKCE with S256
- Protects against authorization code interception

### Scopes
- Base required scopes: `openid`, `profile`, `email`
- App-specific scopes control access to individual Nextcloud apps
- See [OAuth Scopes](#oauth-scopes) section for complete scope reference

### Token Validation
- Every MCP request validates Bearer token
- Cached for performance (1-hour default)
- Calls userinfo endpoint for validation

## OAuth Scopes

The Nextcloud MCP Server implements fine-grained OAuth scopes for each Nextcloud app integration. Scopes control which tools are visible and accessible to users based on their granted permissions.

### Scope-Based Access Control

When using OAuth authentication:
1. **Dynamic Discovery**: The server automatically discovers all required scopes from `@require_scopes` decorators on MCP tools
2. **Tool Filtering**: Tools are dynamically filtered based on the user's token scopes - users only see tools they have permission to use
3. **Per-Tool Enforcement**: Each tool validates required scopes before execution, returning a 403 error if insufficient scopes are present

### Supported Scopes

The server supports the following OAuth scopes, organized by Nextcloud app:

#### Base OIDC Scopes
- `openid` - OpenID Connect authentication (required)
- `profile` - Access to user profile information (required)
- `email` - Access to user email address (required)

#### Notes App
- `notes:read` - Read notes, search notes, get note attachments
- `notes:write` - Create, update, append to, and delete notes

#### Calendar App
- `calendar:read` - List calendars, read events, search events
- `calendar:write` - Create, update, and delete calendars and events

#### Calendar Tasks (VTODO)
- `todo:read` - List and read CalDAV tasks
- `todo:write` - Create, update, and delete CalDAV tasks

#### Contacts App
- `contacts:read` - List address books and read contacts (CardDAV)
- `contacts:write` - Create, update, and delete address books and contacts

#### Cookbook App
- `cookbook:read` - Read recipes, search recipes
- `cookbook:write` - Create, update, and delete recipes

#### Deck App
- `deck:read` - List boards, stacks, cards, and labels
- `deck:write` - Create, update, and delete boards, stacks, cards, and labels

#### Tables App
- `tables:read` - List tables and read rows
- `tables:write` - Create, update, and delete rows in tables

#### Files (WebDAV)
- `files:read` - List files, read file contents, search files
- `files:write` - Upload, update, move, copy, and delete files

#### Sharing
- `sharing:read` - List shares and read share information
- `sharing:write` - Create, update, and delete shares

### Scope Discovery

The MCP server provides scope discovery through two mechanisms:

#### 1. Protected Resource Metadata (PRM) Endpoint
```bash
# Query the PRM endpoint
curl http://localhost:8000/.well-known/oauth-protected-resource/mcp

# Response includes dynamically discovered scopes
{
  "resource": "http://localhost:8000/mcp",
  "scopes_supported": ["openid", "profile", "email", "notes:read", ...],
  "authorization_servers": ["https://nextcloud.example.com"],
  "bearer_methods_supported": ["header"],
  "resource_signing_alg_values_supported": ["RS256"]
}
```

The `scopes_supported` field is **dynamically generated** from all registered MCP tools, ensuring it always reflects the actual available scopes.

#### 2. Scope Enforcement via Decorators

Tools are decorated with `@require_scopes()` to declare their required permissions:

```python
from nextcloud_mcp_server.auth import require_scopes

@mcp.tool()
@require_scopes("notes:read")
async def nc_notes_get_note(ctx: Context, note_id: int):
    """Get a specific note by ID"""
    # Implementation
```

### Client Registration Scopes

During OAuth client registration (dynamic or manual), clients request a set of scopes that define the **maximum allowed** scopes for that client. The actual per-tool enforcement is handled separately via decorators.

**Environment Variable**:
```bash
NEXTCLOUD_OIDC_SCOPES="openid profile email notes:read notes:write calendar:read calendar:write ..."
```

**Default**: All supported scopes (recommended for development)

> **Note**: Client registration scopes define the maximum permissions. The MCP server's PRM endpoint dynamically advertises the actual supported scopes based on registered tools.

### Step-Up Authorization

The server supports OAuth step-up authorization (RFC 8693). If a user attempts to use a tool requiring scopes they don't have:

1. Tool returns `403 Forbidden` with `InsufficientScopeError`
2. Response includes `WWW-Authenticate` header listing missing scopes:
   ```
   WWW-Authenticate: Bearer error="insufficient_scope", scope="notes:write", resource_metadata="..."
   ```
3. Client can re-authorize with additional scopes

### Scope Validation

All scope enforcement happens at two levels:

1. **Tool Visibility**: During `list_tools` requests, only tools matching the user's token scopes are returned
2. **Execution Time**: When calling a tool, the `@require_scopes` decorator validates the token has necessary scopes

**Example**:
```python
# User token has: ["openid", "profile", "email", "notes:read"]
# They will see: 4 read-only notes tools
# They will NOT see: 3 write notes tools (notes:write required)
# Attempting to call a write tool returns 403 Forbidden
```

## Configuration

See [Configuration Guide](configuration.md) for all OAuth environment variables:

| Variable | Purpose |
|----------|---------|
| `NEXTCLOUD_HOST` | Nextcloud instance URL |
| `NEXTCLOUD_OIDC_CLIENT_ID` | Pre-configured client ID (optional) |
| `NEXTCLOUD_OIDC_CLIENT_SECRET` | Pre-configured client secret (optional) |
| `NEXTCLOUD_MCP_SERVER_URL` | MCP server URL for OAuth callbacks |
| `NEXTCLOUD_OIDC_CLIENT_STORAGE` | Path for auto-registered credentials |

## Testing

The integration test suite includes comprehensive OAuth testing:

- **Automated tests** (Playwright): [`tests/client/test_oauth_playwright.py`](../tests/client/test_oauth_playwright.py)
- **Fixtures**: [`tests/conftest.py`](../tests/conftest.py)

Run OAuth tests:
```bash
# Start OAuth-enabled MCP server
docker-compose up --build -d mcp-oauth

# Run automated tests
uv run pytest tests/client/test_oauth_playwright.py --browser firefox -v
```

## See Also

- [OAuth Setup Guide](oauth-setup.md) - Configuration steps
- [OAuth Quick Start](quickstart-oauth.md) - Get started quickly
- [Upstream Status](oauth-upstream-status.md) - Required upstream patches
- [OAuth Troubleshooting](oauth-troubleshooting.md) - Common issues
- [RFC 6749](https://www.rfc-editor.org/rfc/rfc6749) - OAuth 2.0 Authorization Framework
- [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636) - PKCE
- [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html)
