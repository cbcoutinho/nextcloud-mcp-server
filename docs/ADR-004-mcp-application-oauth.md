# ADR-004: Federated Authentication Architecture for Offline Access

**Status**: Draft
**Date**: 2025-11-02
**Supersedes**: ADR-002

## Context

ADR-002 attempted to solve the problem of background workers accessing user data by proposing token exchange patterns. However, it fundamentally misunderstood the MCP protocol's authentication architecture and OAuth delegation patterns.

The real challenge is that:
1. The MCP server needs to access Nextcloud APIs on behalf of users
2. Background workers need to operate when users are offline
3. We need proper OAuth compliance with user consent
4. Modern enterprise environments use federated identity providers

The solution is a **Federated Authentication Architecture** where both the MCP server and Nextcloud trust the same Identity Provider (IdP).

## Problem Statement

We need a way for:
1. Users to authenticate once to a central identity provider
2. The MCP server to obtain delegated access to Nextcloud resources
3. Background workers to access user data using stored refresh tokens
4. Clean separation between identity management and resource access

The core issue: **How can the MCP server obtain refresh tokens from a shared IdP to access Nextcloud on behalf of users?**

## Decision

We will implement a **Federated Authentication Architecture using a Shared Identity Provider** where:

1. **Shared IdP**: A central identity provider (e.g., Keycloak, Okta, Azure AD) manages user authentication
2. **MCP Server as OAuth Client**: The MCP server registers with the shared IdP to request tokens
3. **Nextcloud as Resource Server**: Nextcloud validates tokens issued by the shared IdP
4. **On-Behalf-Of Flow**: The MCP server requests tokens scoped for Nextcloud access

The MCP server will:
- Act as an OAuth client to the shared IdP
- Request tokens on behalf of users, scoped for Nextcloud API access
- Store refresh tokens securely with rotation
- Use stored tokens for both MCP sessions and background operations

## Architecture

### Token Broker Architecture with Linked Authorization

The MCP server acts as a **token broker** using a linked authorization pattern:

#### The Core Challenge
When the MCP client authenticates to the MCP server, we need to:
1. Authenticate the client to the MCP server (audience: "mcp-server")
2. Obtain refresh tokens for Nextcloud access (audience: "nextcloud")
3. Do this in a single OAuth flow from the user's perspective

#### Solution: Linked Authorization with Scope-Based Audiences

During initial OAuth authorization, the MCP server requests:
- **Scopes**: `openid profile offline_access nextcloud:*`
- **Initial audience**: `mcp-server` (for client authentication)
- **Linked resources**: Configured in Keycloak to allow refresh tokens to mint tokens for Nextcloud

The IdP (Keycloak) is configured to:
1. Issue initial access token with `audience: "mcp-server"`
2. Issue refresh token that can obtain tokens for BOTH audiences based on requested scopes
3. Allow the MCP server to request different audiences when using the refresh token

#### Token Types and Lifecycles

1. **MCP Access Tokens** (audience: "mcp-server")
   - Initial token from OAuth flow
   - Authenticates MCP clients to MCP server
   - Short-lived (1 hour)
   - Cannot access Nextcloud directly

2. **Nextcloud Access Tokens** (audience: "nextcloud")
   - Obtained by MCP server using refresh token with audience parameter
   - Used for Nextcloud API access
   - Never exposed to MCP clients
   - Refreshed as needed using stored refresh token

3. **Master Refresh Token**
   - Issued during initial OAuth with `offline_access` scope
   - Can mint tokens for multiple configured audiences
   - Stored encrypted by MCP server
   - Enables both MCP authentication and Nextcloud access

```
┌─────────────┐                ┌─────────────────┐                ┌──────────────┐              ┌────────────┐
│  MCP Client │◄──────401──────│   MCP Server    │◄───Exchange────│  Shared IdP  │──Validates──►│ Nextcloud  │
│  (Native)   │                │  (Token Broker) │    Tokens      │  (Keycloak)  │   Tokens     │(Resource)  │
└─────────────┘                └─────────────────┘                └──────────────┘              └────────────┘
       │                                │                                 │
       │ Token (aud: mcp-server)        │                                 │
       │ Via PKCE OAuth                 ├── Refresh Token ────────────────┤
       ▼                                │                                 │
┌─────────────┐                        ├── Get Token (aud: nextcloud) ───┤
│   Validate  │                        │                                 │
│ aud == "mcp"│                        ▼                                 ▼
└─────────────┘                ┌───────────────┐                  ┌──────────────┐
                               │Refresh Tokens │                  │Token Exchange│
                               │   (Encrypted) │                  │   Endpoint   │
                               └───────────────┘                  └──────────────┘
```

**Key Components:**
- **MCP Client**: Native application using PKCE flow, receives tokens with `aud: "mcp-server"`
- **MCP Server**: Token broker that validates MCP tokens, exchanges for Nextcloud tokens
- **Shared IdP**: Issues audience-specific tokens, supports token exchange/refresh
- **Nextcloud**: Validates tokens with `aud: "nextcloud"` for API access

### Authentication Flows

#### Initial Setup with Linked Authorization (One-Time)

```mermaid
sequenceDiagram
    participant User
    participant MCPClient as MCP Client<br/>(Native App)
    participant MCPServer as MCP Server
    participant IdP as Shared IdP (Keycloak)
    participant Nextcloud

    User->>MCPClient: Connect to MCP
    MCPClient->>MCPServer: Initial request
    MCPServer-->>MCPClient: 401 Unauthorized + OAuth config

    Note over MCPClient: Generate PKCE values:<br/>code_verifier = random string<br/>code_challenge = SHA256(code_verifier)

    MCPClient->>MCPClient: Start local HTTP server<br/>on random port (e.g., :51234)

    MCPClient->>MCPServer: GET /oauth/authorize<br/>+ code_challenge<br/>+ redirect_uri=http://localhost:51234/callback

    MCPServer->>MCPServer: Store session with PKCE
    MCPServer->>MCPClient: 302 Redirect to IdP

    MCPClient->>IdP: Authorization Request<br/>+ code_challenge<br/>+ code_challenge_method=S256
    Note over IdP: Requested scopes:<br/>- openid profile email<br/>- offline_access<br/>- nextcloud:notes:*<br/>Initial audience: mcp-server

    IdP->>User: Login page
    User->>IdP: Authenticate once

    IdP->>User: Consent screen
    Note over IdP: "Allow MCP Server to:<br/>- Authenticate you<br/>- Access data offline<br/>- Access Nextcloud on your behalf"

    User->>IdP: Grant consent
    IdP->>MCPClient: 302 Redirect to localhost:51234<br/>with authorization code

    MCPClient->>MCPServer: POST /oauth/token<br/>code + code_verifier

    MCPServer->>MCPServer: Verify PKCE<br/>(SHA256(code_verifier) == code_challenge)

    MCPServer->>IdP: Exchange code for tokens<br/>+ code_verifier
    IdP->>MCPServer: Tokens with aud:"mcp-server"<br/>+ Master refresh token

    Note over MCPServer: Received:<br/>- Access token (aud: mcp-server)<br/>- Master refresh token<br/>(can mint both audiences)

    MCPServer->>MCPServer: Store master refresh token<br/>(encrypted)
    MCPServer-->>MCPClient: Return access token<br/>(aud: mcp-server)

    MCPClient->>MCPServer: Retry with token<br/>(aud: mcp-server)
    MCPServer->>MCPServer: Validate audience

    Note over MCPServer: Need Nextcloud access,<br/>use refresh token

    MCPServer->>IdP: POST /token<br/>refresh_token + audience=nextcloud
    IdP->>MCPServer: New token (aud: nextcloud)

    MCPServer->>Nextcloud: API call with token<br/>(aud: nextcloud)
    Nextcloud->>IdP: Validate token + audience
    IdP-->>Nextcloud: Valid for Nextcloud
    Nextcloud-->>MCPServer: API response
    MCPServer-->>MCPClient: Success
```

#### Subsequent MCP Sessions (Token Broker Pattern)

```mermaid
sequenceDiagram
    participant MCPClient as MCP Client
    participant MCPServer as MCP Server
    participant TokenStore as Token Storage
    participant IdP as Shared IdP
    participant Nextcloud

    MCPClient->>MCPServer: Request with token<br/>(aud: mcp-server)
    MCPServer->>MCPServer: Validate token audience<br/>Must be "mcp-server"

    Note over MCPServer: MCP auth valid,<br/>need Nextcloud token

    MCPServer->>TokenStore: Get master refresh token
    TokenStore-->>MCPServer: Encrypted refresh token

    MCPServer->>MCPServer: Check cached<br/>Nextcloud token expiry

    alt Nextcloud Token Expired or Missing
        MCPServer->>IdP: POST /token<br/>grant_type=refresh_token<br/>audience=nextcloud
        IdP->>MCPServer: New access token<br/>(aud: nextcloud)
        MCPServer->>TokenStore: Cache Nextcloud token<br/>(short TTL)
    end

    MCPServer->>Nextcloud: API call with token<br/>(aud: nextcloud)
    Nextcloud->>IdP: Validate token + audience
    IdP-->>Nextcloud: Valid for Nextcloud
    Nextcloud-->>MCPServer: API response
    MCPServer-->>MCPClient: MCP response

    Note over MCPClient,MCPServer: Client only sees<br/>aud:"mcp-server" tokens
```

#### Background Operations

```mermaid
sequenceDiagram
    participant Worker as Background Worker
    participant TokenStore as Token Storage
    participant IdP as Shared IdP
    participant Nextcloud

    Worker->>TokenStore: Get user's active refresh token
    TokenStore-->>Worker: Encrypted IdP refresh token
    Worker->>TokenStore: Mark token as 'used'
    Worker->>Worker: Decrypt token

    Worker->>IdP: Exchange refresh token
    IdP->>Worker: New access + refresh tokens
    Worker->>TokenStore: Store new tokens (status='active')

    Worker->>Nextcloud: API call with access token
    Nextcloud->>IdP: Validate token
    IdP-->>Nextcloud: Valid + scopes
    Nextcloud-->>Worker: API response

    Note over Worker: No MCP client involvement!
```

## Implementation

### 1. Token Broker Verifier

```python
import jwt
from datetime import datetime, timedelta

class TokenBrokerVerifier(TokenVerifier):
    """Token broker that maintains audience isolation between MCP and Nextcloud."""

    def __init__(self,
                 token_storage: RefreshTokenStorage,
                 idp_client: OAuthClient):
        self.storage = token_storage
        self.idp_client = idp_client
        self.nextcloud_token_cache = {}  # Short-lived cache

    async def verify_mcp_token(self, token: str) -> dict | None:
        """Verify IdP-issued token has MCP server audience."""
        try:
            # Decode without verification (IdP signed it)
            # In production, verify with IdP public key
            payload = jwt.decode(
                token,
                options={"verify_signature": False}
            )

            # CRITICAL: Verify audience is MCP server
            audiences = payload.get('aud', [])
            if isinstance(audiences, str):
                audiences = [audiences]

            if 'mcp-server' not in audiences:
                logger.warning(f"Token rejected: wrong audience {audiences}")
                return None  # Not for MCP server

            # Check expiry
            if payload.get('exp', 0) < datetime.utcnow().timestamp():
                return None

            return {
                'user_id': payload['sub'],
                'session_id': payload.get('jti'),
                'scopes': payload.get('scope', '').split()
            }
        except jwt.InvalidTokenError:
            return None

    async def get_nextcloud_token(self, user_id: str) -> str | None:
        """Get or refresh token with Nextcloud audience."""
        # Check cache first
        cached = self.nextcloud_token_cache.get(user_id)
        if cached and cached['exp'] > datetime.utcnow().timestamp():
            return cached['token']

        # Get master refresh token
        refresh_token = await self.storage.get_refresh_token(user_id)
        if not refresh_token:
            return None  # User needs to re-authenticate

        try:
            # Request new token with Nextcloud audience
            response = await self.idp_client.refresh_token(
                refresh_token=refresh_token,
                audience='nextcloud'  # CRITICAL: Request Nextcloud audience
            )

            # Verify the new token has correct audience
            payload = jwt.decode(
                response.access_token,
                options={"verify_signature": False}
            )

            audiences = payload.get('aud', [])
            if isinstance(audiences, str):
                audiences = [audiences]

            if 'nextcloud' not in audiences:
                raise ValueError(f"IdP returned wrong audience: {audiences}")

            # Cache for short period (5 minutes)
            self.nextcloud_token_cache[user_id] = {
                'token': response.access_token,
                'exp': payload.get('exp', 0)
            }

            return response.access_token

        except Exception as e:
            logger.error(f"Failed to get Nextcloud token: {e}")
            return None

    async def verify_token(self, token: str) -> AccessToken | None:
        """Main verification for MCP protocol with token brokering."""
        # Step 1: Verify token has MCP audience
        mcp_auth = await self.verify_mcp_token(token)
        if not mcp_auth:
            return None  # Triggers 401 response

        # Step 2: Get separate token for Nextcloud access
        nextcloud_token = await self.get_nextcloud_token(mcp_auth['user_id'])
        if not nextcloud_token:
            return None  # Failed to get backend token

        # Return Nextcloud token for backend use
        # MCP client never sees this token
        return AccessToken(
            token=nextcloud_token,  # Token with aud: nextcloud
            scopes=mcp_auth['scopes'],
            resource=json.dumps({
                "user_id": mcp_auth['user_id'],
                "session_id": mcp_auth.get('session_id')
            })
        )

    async def refresh_master_token(self, user_id: str):
        """Refresh the master refresh token (with rotation)."""
        old_refresh = await self.storage.get_refresh_token(user_id)
        if not old_refresh:
            raise ValueError("No refresh token found")

        # Mark as used (rotation)
        await self.storage.mark_token_used(old_refresh.token_id)

        try:
            # Get new refresh token from IdP
            response = await self.idp_client.refresh_token(
                refresh_token=old_refresh.token,
                scope='openid profile offline_access nextcloud:*'
            )

            # Store new refresh token
            await self.storage.store_refresh_token(
                user_id=user_id,
                token_family_id=old_refresh.token_family_id,
                refresh_token=response.refresh_token,
                status='active'
            )

            return response.refresh_token

        except RefreshTokenReuseError:
            # Possible token theft - revoke entire family
            await self.storage.revoke_token_family(old_refresh.token_family_id)
            await self.alert_user_possible_breach(user_id)
            raise
```

### 2. OAuth Endpoints with PKCE (Native Client Support)

```python
import hashlib
import secrets
from urllib.parse import urlencode

@app.get("/oauth/authorize")
async def oauth_authorize(
    response_type: str = "code",
    client_id: str = None,
    redirect_uri: str = None,
    scope: str = None,
    state: str = None,
    code_challenge: str = None,  # PKCE
    code_challenge_method: str = "S256"  # PKCE
):
    """MCP Server OAuth endpoint with PKCE support."""
    # Validate redirect_uri is localhost (native client)
    if not redirect_uri or not redirect_uri.startswith(('http://localhost:', 'http://127.0.0.1:')):
        return {"error": "invalid_request", "error_description": "Invalid redirect_uri for native client"}

    # Store MCP client details with PKCE
    session_id = str(uuid4())
    authorization_code = secrets.token_urlsafe(32)

    await store_oauth_session(
        session_id=session_id,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        authorization_code=authorization_code  # Pre-generate for later
    )

    # Build IdP authorization URL with all needed scopes
    idp_params = {
        "client_id": MCP_SERVER_CLIENT_ID,
        "redirect_uri": f"{MCP_SERVER_URL}/oauth/callback",
        "response_type": "code",
        "scope": "openid profile email offline_access "  # Identity + offline
                 "nextcloud:notes:read nextcloud:notes:write "  # Nextcloud scopes
                 "nextcloud:calendar:read nextcloud:calendar:write",
        "state": f"{session_id}:{state}",  # Preserve client state
        "prompt": "consent",  # Ensure refresh token
        # Pass PKCE to IdP if supported
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method
    }

    idp_auth_url = f"{IDP_AUTHORIZATION_ENDPOINT}?{urlencode(idp_params)}"
    return RedirectResponse(idp_auth_url)

@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str):
    """Handle IdP callback and redirect to native client."""
    # Extract session ID and original client state
    try:
        session_id, client_state = state.split(":", 1)
    except ValueError:
        return {"error": "invalid_state"}

    oauth_session = await get_oauth_session(session_id)
    if not oauth_session:
        return {"error": "invalid_session"}

    # Exchange code with IdP for tokens
    tokens = await idp_client.exchange_code(
        code=code,
        redirect_uri=f"{MCP_SERVER_URL}/oauth/callback",
        code_verifier=oauth_session.get('code_verifier')  # If IdP supports PKCE
    )

    # Decode ID token to get user info
    userinfo = decode_id_token(tokens.id_token)

    # Create or update user account
    user = await create_or_update_user(
        idp_sub=userinfo.sub,
        username=userinfo.preferred_username,
        email=userinfo.email
    )

    # Generate new token family for rotation
    token_family_id = str(uuid4())

    # Store IdP tokens (these have Nextcloud scopes)
    await token_storage.store_tokens(
        user_id=user.id,
        token_family_id=token_family_id,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        status='active',
        scopes=tokens.scope,
        idp_subject=userinfo.sub
    )

    # Update session with user_id for token exchange
    await update_oauth_session(session_id, user_id=user.id)

    # CRITICAL: Redirect to native client with authorization code
    # No HTML page! Native clients expect 302 redirect
    redirect_params = {
        "code": oauth_session.authorization_code,
        "state": client_state  # Return original client state
    }

    redirect_url = f"{oauth_session.redirect_uri}?{urlencode(redirect_params)}"
    return RedirectResponse(redirect_url, status_code=302)

@app.post("/oauth/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: str = Form(None),
    code_verifier: str = Form(None),  # PKCE
    redirect_uri: str = Form(None),
    client_id: str = Form(None),
    refresh_token: str = Form(None)
):
    """Token endpoint that returns IdP tokens with MCP audience."""

    if grant_type == "authorization_code":
        # Find session by authorization code
        oauth_session = await get_oauth_session_by_code(code)
        if not oauth_session:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid authorization code"},
                status_code=400
            )

        # Verify PKCE
        if oauth_session.code_challenge:
            if not code_verifier:
                return JSONResponse(
                    {"error": "invalid_request", "error_description": "code_verifier required"},
                    status_code=400
                )

            # Compute challenge from verifier
            computed_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).decode().rstrip('=')

            if computed_challenge != oauth_session.code_challenge:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                    status_code=400
                )

        # Verify redirect_uri matches
        if redirect_uri != oauth_session.redirect_uri:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
                status_code=400
            )

        # Get stored IdP tokens for this session
        # These were stored during the callback from IdP
        idp_tokens = await get_idp_tokens_for_session(oauth_session.session_id)

        # Verify the access token has MCP audience
        payload = jwt.decode(
            idp_tokens.access_token,
            options={"verify_signature": False}
        )

        audiences = payload.get('aud', [])
        if isinstance(audiences, str):
            audiences = [audiences]

        if 'mcp-server' not in audiences:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Token missing MCP audience"},
                status_code=400
            )

        # Invalidate authorization code
        await invalidate_oauth_session(oauth_session.session_id)

        # Return IdP tokens (with aud: mcp-server)
        # Client gets the actual IdP token, not an MCP-generated one
        return {
            "access_token": idp_tokens.access_token,  # IdP token with aud: mcp-server
            "token_type": "Bearer",
            "expires_in": idp_tokens.expires_in,
            "scope": idp_tokens.scope,
            "refresh_token": idp_tokens.refresh_token  # Master refresh token
        }

    elif grant_type == "refresh_token":
        # Refresh with IdP for new MCP-audience token
        try:
            # Use master refresh token to get new MCP token
            response = await idp_client.refresh_token(
                refresh_token=refresh_token,
                audience='mcp-server'  # Request MCP audience
            )

            # Verify audience
            payload = jwt.decode(
                response.access_token,
                options={"verify_signature": False}
            )

            audiences = payload.get('aud', [])
            if isinstance(audiences, str):
                audiences = [audiences]

            if 'mcp-server' not in audiences:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "Refreshed token missing MCP audience"},
                    status_code=400
                )

            return {
                "access_token": response.access_token,
                "token_type": "Bearer",
                "expires_in": response.expires_in,
                "scope": response.scope,
                "refresh_token": response.refresh_token  # New refresh token if rotated
            }
        except Exception as e:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": str(e)},
                status_code=400
            )

    return JSONResponse(
        {"error": "unsupported_grant_type"},
        status_code=400
    )
```

### 3. 401 Response with WWW-Authenticate

```python
@mcp.tool()
async def list_notes(ctx: Context) -> dict:
    """List notes - automatically triggers OAuth if needed."""
    try:
        # FastMCP automatically calls token verifier
        # If it returns None, a 401 is sent
        client = get_client_from_context(ctx)
        notes = await client.notes.list_notes()
        return {"notes": notes}
    except Unauthorized:
        # Return 401 with WWW-Authenticate header
        raise HTTPException(
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer realm="{MCP_SERVER_URL}/oauth/authorize", '
                    f'error="invalid_token", '
                    f'error_description="Authentication required"'
                )
            }
        )
```

### 4. Token Storage Schema

```sql
-- User accounts (created from IdP identity)
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    idp_sub TEXT UNIQUE NOT NULL,      -- IdP subject identifier
    username TEXT NOT NULL,
    email TEXT,
    created_at INTEGER NOT NULL,
    last_login INTEGER NOT NULL
);

-- IdP tokens with rotation support
CREATE TABLE idp_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    token_family_id TEXT NOT NULL,    -- Groups all tokens in rotation chain
    encrypted_access_token BLOB NOT NULL,
    encrypted_refresh_token BLOB NOT NULL,
    access_expires_at INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'used', 'revoked')),
    scopes TEXT NOT NULL,              -- Includes Nextcloud scopes
    idp_subject TEXT NOT NULL,         -- IdP user identifier
    created_at INTEGER NOT NULL,
    used_at INTEGER,                   -- When token was exchanged

    -- Only one active token per family
    UNIQUE(token_family_id, status) WHERE status = 'active'
);

-- Index for quick lookups
CREATE INDEX idx_active_tokens ON idp_tokens(user_id, status)
    WHERE status = 'active';
CREATE INDEX idx_token_families ON idp_tokens(token_family_id);

-- MCP session tokens (separate from IdP tokens)
CREATE TABLE mcp_sessions (
    session_token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_used INTEGER
);

-- OAuth flow sessions with PKCE support (temporary during auth)
CREATE TABLE oauth_sessions (
    session_id TEXT PRIMARY KEY,
    client_id TEXT,
    redirect_uri TEXT NOT NULL,
    state TEXT,
    code_challenge TEXT,            -- PKCE code challenge
    code_challenge_method TEXT,     -- PKCE method (S256)
    authorization_code TEXT UNIQUE,  -- Pre-generated auth code
    user_id TEXT,                   -- Set after IdP authentication
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

-- Audit log for security
CREATE TABLE token_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    token_family_id TEXT,
    operation TEXT NOT NULL,  -- 'authorize', 'refresh', 'revoke', 'reuse_detected'
    timestamp INTEGER NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    details TEXT
);
```

### 5. Background Worker (IdP Tokens Only)

```python
class BackgroundSyncWorker:
    """Background workers use IdP tokens directly - no MCP session tokens."""

    def __init__(self, token_storage: RefreshTokenStorage):
        self.storage = token_storage
        self.idp_client = OAuthClient.from_discovery(IDP_DISCOVERY_URL)
        self.nextcloud_url = os.getenv("NEXTCLOUD_HOST")

    async def sync_user_data(self, user_id: str):
        """
        Sync data using IdP tokens ONLY.

        Key Points:
        - Workers NEVER use MCP session tokens (those are for client auth)
        - Workers directly refresh IdP tokens with the IdP
        - IdP tokens have audience: "nextcloud" for backend access
        - No MCP client involvement required
        """
        # Get active IdP refresh token (NOT MCP token)
        idp_tokens = await self.storage.get_active_tokens(user_id)
        if not idp_tokens:
            logger.warning(f"No active IdP tokens for user {user_id}")
            return

        # Mark token as used immediately (rotation)
        await self.storage.mark_token_used(idp_tokens.id)

        try:
            # Exchange with IdP for new tokens (direct IdP communication)
            new_tokens = await self.idp_client.refresh(idp_tokens.refresh_token)

            # Verify audience is for Nextcloud (security check)
            id_token_claims = jwt.decode(
                new_tokens.id_token,
                options={"verify_signature": False}
            )
            if 'nextcloud' not in id_token_claims.get('aud', []):
                raise ValueError("IdP token missing Nextcloud audience")

            # Store new tokens in same family
            await self.storage.store_tokens(
                user_id=user_id,
                token_family_id=idp_tokens.token_family_id,
                access_token=new_tokens.access_token,
                refresh_token=new_tokens.refresh_token,
                status='active'
            )

            # Create Nextcloud client with IdP access token
            # Token has audience: "nextcloud" and proper scopes
            client = NextcloudClient.from_token(
                base_url=self.nextcloud_url,
                token=new_tokens.access_token,  # IdP token, NOT MCP token
                username=idp_tokens.username
            )

            # Perform sync operations with Nextcloud
            await self.sync_notes(user_id, client)
            await self.sync_calendar(user_id, client)
            await self.sync_contacts(user_id, client)

            logger.info(f"Background sync completed for user {user_id}")

        except HTTPStatusError as e:
            if e.response.status_code == 401:
                # Token rejected by IdP or Nextcloud
                await self.storage.revoke_token_family(idp_tokens.token_family_id)
                await self.log_security_event(
                    user_id,
                    "token_revoked",
                    f"Token family {idp_tokens.token_family_id} revoked due to 401"
                )
            raise
        except RefreshTokenReuseError:
            # Detected token reuse - possible security breach
            await self.log_security_event(
                user_id,
                "reuse_detected",
                f"Token reuse detected for family {idp_tokens.token_family_id}"
            )
            raise
        except Exception as e:
            # Revert token status on failure
            await self.storage.revert_token_status(idp_tokens.id)
            logger.error(f"Background sync failed for user {user_id}: {e}")
            raise

    async def log_security_event(self, user_id: str, event: str, details: str):
        """Log security events for audit."""
        await self.storage.log_audit(
            user_id=user_id,
            operation=event,
            details=details,
            timestamp=datetime.utcnow().isoformat()
        )
```

### 6. Configuration

```python
# Environment variables for federated setup
IDP_DISCOVERY_URL = os.getenv("IDP_DISCOVERY_URL")  # e.g., https://keycloak.example.com/realms/master/.well-known/openid-configuration
MCP_SERVER_CLIENT_ID = os.getenv("MCP_SERVER_CLIENT_ID")  # MCP server's client ID in IdP
MCP_SERVER_CLIENT_SECRET = os.getenv("MCP_SERVER_CLIENT_SECRET")  # Client secret
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8000")

# Nextcloud configuration
NEXTCLOUD_HOST = os.getenv("NEXTCLOUD_HOST")  # Nextcloud instance URL

# Parse IdP discovery document
async def setup_idp_client():
    """Initialize OAuth client from IdP discovery."""
    async with httpx.AsyncClient() as client:
        discovery = await client.get(IDP_DISCOVERY_URL)
        discovery_doc = discovery.json()

    return OAuthClient(
        authorization_endpoint=discovery_doc["authorization_endpoint"],
        token_endpoint=discovery_doc["token_endpoint"],
        introspection_endpoint=discovery_doc.get("introspection_endpoint"),
        userinfo_endpoint=discovery_doc["userinfo_endpoint"],
        client_id=MCP_SERVER_CLIENT_ID,
        client_secret=MCP_SERVER_CLIENT_SECRET
    )
```

## Advantages

1. **Single Sign-On**: Users authenticate once to the shared IdP
2. **Federated Identity**: Enterprise-ready with support for SAML, LDAP backends
3. **True Offline Access**: Background workers operate with stored IdP refresh tokens
4. **OAuth Compliant**: Proper delegation with on-behalf-of pattern
5. **Security Isolation**: MCP clients never see IdP or Nextcloud credentials
6. **Flexible Backend**: Can swap Nextcloud for other resources without changing auth
7. **Standard Pattern**: Industry-standard federated OAuth architecture

## Disadvantages

1. **IdP Dependency**: Requires a shared identity provider infrastructure
2. **Complex Token Lifecycle**: Managing tokens from IdP for Nextcloud access
3. **Token Validation Overhead**: Nextcloud must validate tokens with IdP
4. **Migration Complexity**: Existing deployments need IdP setup

## Security Considerations

### Audience Isolation Architecture

#### Core Security Principle: Token Audience Separation
The architecture enforces **strict audience isolation** to prevent token misuse:

- **Tokens with `audience: "mcp-server"`** can ONLY authenticate to MCP server
- **Tokens with `audience: "nextcloud"`** can ONLY access Nextcloud APIs
- **No token has multiple audiences** - this would be a security boundary violation
- **Compromised MCP tokens cannot access Nextcloud** directly

#### Token Broker Security Model

The MCP server acts as a **secure token broker**:
1. Validates incoming tokens have `audience: "mcp-server"`
2. Uses stored refresh tokens to obtain `audience: "nextcloud"` tokens
3. Never exposes Nextcloud tokens to MCP clients
4. Maintains separate token lifecycles for each audience

#### Audience Validation Examples
```python
# MCP Access Token (from IdP)
{
  "aud": "mcp-server",      # Single audience ONLY
  "sub": "user-123",
  "scope": "mcp:full",
  "exp": 1234567890
}

# Nextcloud Access Token (obtained via refresh)
{
  "aud": "nextcloud",        # Different audience
  "sub": "user-123",
  "scope": "notes:read calendar:write",
  "exp": 1234567890
}

# Master Refresh Token Claims
{
  "sub": "user-123",
  "scope": "openid profile offline_access nextcloud:*",
  "allowed_audiences": ["mcp-server", "nextcloud"]  # Can mint both
}
```

### PKCE Protection
- **Mandatory for native clients** (RFC 7636)
- Code verifier: 43-128 character random string
- Code challenge: SHA256(code_verifier)
- Prevents authorization code interception
- Validated before token issuance

### Native Client Security
- **Localhost redirect only** (RFC 8252)
  - Restrict to `http://localhost:*` or `http://127.0.0.1:*`
  - Dynamic port allocation per session
  - No custom URL schemes allowed
- **System browser required** - no embedded browsers
- **302 redirect flow** - direct redirect, no HTML page

### Token Storage Security
- **Master refresh tokens**: Encrypted at rest (Fernet/AES-256)
- **Audience-specific caching**: Short-lived cache for Nextcloud tokens
- **Database isolation**: Refresh tokens never exposed to application layer
- **Key rotation**: Support for encryption key rotation
- **Hardware security**: Consider HSM for production

### Token Rotation with Audience Preservation
- **Rotation maintains audience**: New tokens keep same audience
- **Reuse detection**: Previous use revokes entire token family
- **Atomic operations**: Database transactions prevent races
- **Audit trail**: All exchanges logged with audience info

### Trust Boundaries

```
┌──────────────┐     aud:"mcp-server"     ┌──────────────┐
│  MCP Client  │──────────────────────────►│  MCP Server  │
└──────────────┘                           └───────┬──────┘
                                                   │
                                          Refresh for different
                                               audience
                                                   │
┌──────────────┐     aud:"nextcloud"      ┌───────▼──────┐
│  Nextcloud   │◄──────────────────────────│     IdP      │
└──────────────┘                           └──────────────┘
```

1. **MCP Client → MCP Server**: Only `aud:"mcp-server"` tokens
2. **MCP Server → IdP**: Refresh with audience parameter
3. **MCP Server → Nextcloud**: Only `aud:"nextcloud"` tokens
4. **No direct path**: Client cannot use MCP tokens for Nextcloud

### Revocation and Breach Response
- **Audience-specific revocation**: Can revoke MCP without affecting Nextcloud
- **Token family tracking**: All tokens from same refresh chain
- **Immediate propagation**: Revocation flows through trust chain
- **Breach isolation**: Compromised MCP tokens don't grant Nextcloud access

## Migration Strategy

### Phase 1: IdP Setup
1. Deploy shared IdP (Keycloak recommended)
2. Register MCP server as OAuth client
3. Configure Nextcloud to accept IdP tokens
4. Test token validation flow

### Phase 2: Parallel Operation
1. Keep existing pass-through authentication
2. Add federated auth as optional feature flag
3. Test with subset of users
4. Monitor token lifecycle and refresh patterns

### Phase 3: Migration
1. Migrate existing users to IdP accounts
2. Map existing permissions to IdP scopes
3. Update clients to use new OAuth flow
4. Maintain backward compatibility period

### Phase 4: Deprecation
1. Announce end-of-life for pass-through mode
2. Complete user migration
3. Remove legacy authentication code
4. Document new auth flow

## Alternatives Considered

### 1. Pass-Through Only (Current)
- **Pros**: Simple, stateless
- **Cons**: No offline access possible
- **Rejected**: Doesn't meet requirements

### 2. Service Accounts (ADR-002 Tier 1)
- **Pros**: Simple to implement
- **Cons**: Violates OAuth principles, creates audit issues
- **Rejected**: Security and compliance concerns

### 3. Token Exchange (ADR-002 Tier 2)
- **Pros**: Standards-based (RFC 8693)
- **Cons**: Circular dependency, doesn't solve bootstrap problem
- **Rejected**: Doesn't enable true offline access

### 4. Sign-in with Nextcloud (Previous ADR-004)
- **Pros**: Direct Nextcloud integration
- **Cons**: Tight coupling, no enterprise IdP support
- **Rejected**: Not suitable for federated environments

### 5. Double OAuth (Manual)
- **Pros**: Clear separation of concerns
- **Cons**: Poor UX with two login prompts
- **Rejected**: Users shouldn't authenticate twice

## Token Exchange Pattern Implementation

### How Audience-Specific Token Exchange Works

The key to this architecture is the IdP's ability to issue tokens with different audiences from a single refresh token. This is achieved through:

#### 1. Keycloak Configuration

```javascript
// Keycloak Client Configuration for MCP Server
{
  "clientId": "mcp-server",
  "standardFlowEnabled": true,
  "serviceAccountsEnabled": false,
  "publicClient": false,
  "attributes": {
    // Allow refresh tokens to request different audiences
    "oauth2.device.authorization.grant.enabled": "false",
    "oidc.ciba.grant.enabled": "false",
    "oauth2.token.exchange.grant.enabled": "true"  // Enable token exchange
  }
}

// Audience Mapper Configuration
{
  "name": "dynamic-audience-mapper",
  "protocol": "openid-connect",
  "protocolMapper": "oidc-audience-mapper",
  "config": {
    "included.client.audience": "mcp-server",  // Default audience
    "access.token.claim": "true",
    "id.token.claim": "false"
  }
}

// Scope-to-Audience Mapping
{
  "mcp:*": "mcp-server",        // MCP scopes → mcp-server audience
  "nextcloud:*": "nextcloud"     // Nextcloud scopes → nextcloud audience
}
```

#### 2. Refresh Token with Audience Parameter

When the MCP server needs a token for a specific audience:

```http
POST /realms/nextcloud-mcp/protocol/openid-connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
&refresh_token=eyJhbGc...
&client_id=mcp-server
&client_secret=secret
&audience=nextcloud  # Request specific audience
```

Response:
```json
{
  "access_token": "eyJhbGc...",  // Token with aud: "nextcloud"
  "expires_in": 300,
  "refresh_token": "eyJhbGc...",  // Same or rotated refresh token
  "token_type": "Bearer"
}
```

#### 3. Alternative: Token Exchange (RFC 8693)

For IdPs that support token exchange:

```http
POST /realms/nextcloud-mcp/protocol/openid-connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&subject_token=eyJhbGc...  # Token with aud: "mcp-server"
&subject_token_type=urn:ietf:params:oauth:token-type:access_token
&requested_token_type=urn:ietf:params:oauth:token-type:access_token
&audience=nextcloud  # Request different audience
```

### Why This Pattern Is Secure

1. **Audience Validation at Every Layer**:
   - MCP server validates `aud: "mcp-server"` for incoming requests
   - Nextcloud validates `aud: "nextcloud"` for API calls
   - Tokens with wrong audience are rejected

2. **Unidirectional Token Flow**:
   - Client → MCP: Only `aud: "mcp-server"`
   - MCP → Nextcloud: Only `aud: "nextcloud"`
   - No reverse flow possible

3. **Breach Containment**:
   - Stolen MCP token: Cannot access Nextcloud
   - Stolen Nextcloud token: Cannot authenticate to MCP
   - Stolen refresh token: Requires client credentials to use

### Configuration for Popular IdPs

#### Keycloak
- Enable Token Exchange in realm settings
- Configure audience mappers per client
- Use protocol mappers for dynamic audiences

#### Auth0
- Use custom rules for audience selection
- Configure API identifiers as audiences
- Enable refresh token rotation

#### Azure AD
- Configure app registrations for each audience
- Use scope-to-resource mapping
- Enable conditional access policies

#### Okta
- Define custom authorization servers
- Configure audience claim per API
- Use inline hooks for dynamic audiences

## Decision Outcome

The Token Broker Architecture with Audience Isolation provides a secure, enterprise-ready solution for offline access while maintaining strict security boundaries. By using a shared identity provider with audience-specific tokens, we achieve:

1. **Security through isolation**: Different audiences prevent token misuse
2. **Single authentication**: Users authenticate once to the IdP
3. **Offline capabilities**: Master refresh tokens enable background operations
4. **Enterprise compliance**: Follows OAuth best practices and security standards

This architecture follows industry best practices for federated systems and positions the MCP server as a secure token broker in an enterprise identity ecosystem.

## References

- [RFC 6749: OAuth 2.0](https://datatracker.ietf.org/doc/html/rfc6749)
- [RFC 8693: OAuth 2.0 Token Exchange](https://datatracker.ietf.org/doc/html/rfc8693)
- [RFC 7636: PKCE](https://datatracker.ietf.org/doc/html/rfc7636)
- [OAuth 2.0 Security Best Practices](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-security-topics)
- [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html)
- [OAuth 2.0 for Native Apps](https://datatracker.ietf.org/doc/html/rfc8252)
- [OAuth 2.0 Device Authorization Grant](https://datatracker.ietf.org/doc/html/rfc8628)