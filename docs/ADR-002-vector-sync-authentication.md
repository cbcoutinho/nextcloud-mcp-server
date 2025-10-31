# ADR-002: Vector Database Background Sync Authentication

## Status
Proposed

## Context

To enable semantic search capabilities, the MCP server needs to index user content (notes, files, calendar events) into a vector database. This requires a background sync worker that:

1. **Runs independently** of user requests (periodic or continuous operation)
2. **Accesses multiple users' content** to build a comprehensive search index
3. **Respects user permissions** - only index content users have access to
4. **Operates in OAuth mode** - where the MCP server doesn't have traditional admin credentials

### Current OAuth Architecture

The MCP server currently operates in two authentication modes:

1. **BasicAuth Mode**: Uses username/password credentials (typically admin account)
2. **OAuth Mode**: Single OAuth client, multiple user tokens
   - Users authenticate via OAuth flow
   - Each request includes user's access token
   - Server creates per-request `NextcloudClient` with user's bearer token
   - No tokens are stored server-side

### The Challenge

Background workers need long-lived authentication to:
- Index content continuously/periodically
- Process multiple users' data in batch operations
- Operate when users are not actively making requests

However, in OAuth mode:
- User access tokens are ephemeral (exist only during request)
- MCP server doesn't store user credentials
- Admin credentials defeat the purpose of OAuth

We need an OAuth-native solution that maintains security while enabling background operations.

## Decision

We will implement a **tiered authentication strategy** that leverages OAuth standards with graceful fallback:

### Primary Strategy: OAuth-Based Authentication

**Tier 1: Offline Access with Refresh Tokens** (Preferred)
- Request `offline_access` scope during OAuth client registration
- Receive and securely store user refresh tokens
- Background worker exchanges refresh tokens for access tokens as needed
- Respects per-user permissions and provides full audit trail

**Tier 2: Token Exchange (RFC 8693)** (If supported)
- Service account exchanges its token for user-scoped tokens on-demand
- No token storage required
- Only available if OIDC provider implements RFC 8693

### Fallback Strategy: Admin Credentials

**Tier 3: Admin BasicAuth** (Development/Simple Deployments)
- Dedicated sync account with read-only permissions
- Clear documentation of security implications
- Recommended only for trusted environments

### Key Architectural Principles

1. **Capability Detection**: Automatically detect which OAuth methods are supported
2. **Dual-Phase Authorization**:
   - Sync worker indexes with service credentials
   - User requests verify access with user's OAuth token
3. **Defense in Depth**: Vector database is search accelerator, not security boundary
4. **Separation of Concerns**: Sync credentials ≠ Request credentials

## Implementation Details

### 1. Offline Access Flow (Tier 1)

#### 1.1 Client Registration
```python
# During OAuth client registration
client_metadata = {
    "client_name": "Nextcloud MCP Server",
    "redirect_uris": ["http://localhost:8000/oauth/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "scope": "openid profile email offline_access notes:read files:read ...",
    "token_type": "Bearer"  # or "jwt"
}
```

#### 1.2 Token Storage
```python
# Encrypted token storage
class RefreshTokenStorage:
    """Securely store and manage user refresh tokens"""

    def __init__(self, db_path: str, encryption_key: bytes):
        self.db = Database(db_path)
        self.cipher = Fernet(encryption_key)

    async def store_refresh_token(
        self,
        user_id: str,
        refresh_token: str,
        expires_at: int | None = None
    ):
        """Store encrypted refresh token for user"""
        encrypted_token = self.cipher.encrypt(refresh_token.encode())
        await self.db.execute(
            "INSERT OR REPLACE INTO refresh_tokens VALUES (?, ?, ?, ?)",
            (user_id, encrypted_token, expires_at, int(time.time()))
        )

    async def get_refresh_token(self, user_id: str) -> str | None:
        """Retrieve and decrypt refresh token"""
        row = await self.db.fetch_one(
            "SELECT encrypted_token FROM refresh_tokens WHERE user_id = ?",
            (user_id,)
        )
        if row:
            return self.cipher.decrypt(row[0]).decode()
        return None
```

#### 1.3 Token Refresh Flow
```python
async def get_user_access_token(user_id: str) -> str:
    """Exchange refresh token for fresh access token"""

    # Retrieve stored refresh token
    refresh_token = await token_storage.get_refresh_token(user_id)
    if not refresh_token:
        raise ValueError(f"No refresh token for user {user_id}")

    # Exchange for access token
    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token
            },
            auth=(client_id, client_secret)
        )
        response.raise_for_status()
        token_data = response.json()

        # Store new refresh token if rotated
        if "refresh_token" in token_data:
            await token_storage.store_refresh_token(
                user_id,
                token_data["refresh_token"],
                token_data.get("refresh_expires_in")
            )

        return token_data["access_token"]
```

#### 1.4 Capturing Refresh Tokens

**Challenge**: MCP protocol doesn't expose refresh tokens to server

**Solution**: Intercept OAuth callback
```python
# Add route to MCP server
@app.route("/oauth/callback")
async def oauth_callback(request):
    """Capture OAuth callback and store refresh token"""

    code = request.query_params.get("code")
    state = request.query_params.get("state")

    # Exchange authorization code for tokens
    token_response = await exchange_authorization_code(code)

    # Extract user info
    userinfo = await get_userinfo(token_response["access_token"])
    user_id = userinfo["sub"]

    # Store refresh token (if present)
    if "refresh_token" in token_response:
        await token_storage.store_refresh_token(
            user_id,
            token_response["refresh_token"],
            expires_at=token_response.get("refresh_expires_in")
        )
        logger.info(f"Stored refresh token for user: {user_id}")

    # Continue MCP OAuth flow
    return redirect_to_mcp_client(state, token_response)
```

### 2. Token Exchange Flow (Tier 2)

#### 2.1 Capability Detection
```python
async def check_token_exchange_support(discovery_url: str) -> bool:
    """Check if OIDC provider supports RFC 8693 token exchange"""

    async with httpx.AsyncClient() as client:
        response = await client.get(discovery_url)
        discovery = response.json()

        # Check for token exchange grant type
        grant_types = discovery.get("grant_types_supported", [])
        return "urn:ietf:params:oauth:grant-type:token-exchange" in grant_types
```

#### 2.2 Token Exchange Implementation
```python
async def exchange_for_user_token(
    service_token: str,
    user_id: str,
    scopes: list[str]
) -> str:
    """Exchange service token for user-scoped token"""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": service_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "resource": f"user:{user_id}",
                "scope": " ".join(scopes)
            },
            auth=(client_id, client_secret)
        )

        if response.status_code != 200:
            logger.warning(f"Token exchange failed: {response.status_code}")
            raise TokenExchangeNotSupportedError()

        return response.json()["access_token"]
```

#### 2.3 Service Account Token
```python
async def get_service_token() -> str:
    """Get token for MCP server's service account"""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "scope": "notes:read files:read calendar:read"
            },
            auth=(client_id, client_secret)
        )
        response.raise_for_status()
        return response.json()["access_token"]
```

### 3. Sync Worker with Tiered Authentication

```python
# nextcloud_mcp_server/sync_worker.py
class VectorSyncWorker:
    """Background worker for indexing content into vector database"""

    def __init__(self):
        self.auth_method = None
        self.token_storage = None
        self.vector_service = None

    async def initialize(self):
        """Detect and configure authentication method"""

        # Try Tier 1: Offline Access
        if os.getenv("ENABLE_OFFLINE_ACCESS") == "true":
            try:
                encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY")
                self.token_storage = RefreshTokenStorage(
                    db_path="tokens.db",
                    encryption_key=base64.b64decode(encryption_key)
                )
                self.auth_method = "offline_access"
                logger.info("✓ Using offline_access authentication")
                return
            except Exception as e:
                logger.warning(f"Offline access unavailable: {e}")

        # Try Tier 2: Token Exchange
        try:
            if await check_token_exchange_support(discovery_url):
                self.auth_method = "token_exchange"
                logger.info("✓ Using token exchange authentication (RFC 8693)")
                return
        except Exception as e:
            logger.warning(f"Token exchange unavailable: {e}")

        # Fallback: Admin Credentials
        if os.getenv("NEXTCLOUD_USERNAME") and os.getenv("NEXTCLOUD_PASSWORD"):
            self.auth_method = "admin_basic"
            logger.warning(
                "⚠ Using admin BasicAuth authentication. "
                "Consider enabling offline_access for production."
            )
            return

        raise RuntimeError("No authentication method available for sync worker")

    async def get_user_client(self, user_id: str) -> NextcloudClient:
        """Get authenticated client for user based on auth method"""

        if self.auth_method == "offline_access":
            # Exchange refresh token for access token
            access_token = await get_user_access_token(user_id)
            return NextcloudClient.from_token(
                base_url=nextcloud_host,
                token=access_token,
                username=user_id
            )

        elif self.auth_method == "token_exchange":
            # Get service token and exchange for user token
            service_token = await get_service_token()
            user_token = await exchange_for_user_token(
                service_token,
                user_id,
                scopes=["notes:read", "files:read"]
            )
            return NextcloudClient.from_token(
                base_url=nextcloud_host,
                token=user_token,
                username=user_id
            )

        elif self.auth_method == "admin_basic":
            # Use admin credentials (fallback)
            return NextcloudClient.from_env()

        raise RuntimeError(f"Unknown auth method: {self.auth_method}")

    async def sync_user_content(self, user_id: str):
        """Index a user's content into vector database"""

        try:
            # Get authenticated client for this user
            client = await self.get_user_client(user_id)

            # Sync notes
            notes = await client.notes.list_notes()
            for note in notes:
                embedding = await self.vector_service.embed(note.content)
                await self.vector_service.upsert(
                    collection="nextcloud_content",
                    id=f"note_{note.id}",
                    vector=embedding,
                    metadata={
                        "user_id": user_id,
                        "content_type": "note",
                        "note_id": note.id,
                        "title": note.title,
                        "category": note.category
                    }
                )

            logger.info(f"Synced {len(notes)} notes for user: {user_id}")

        except Exception as e:
            logger.error(f"Failed to sync user {user_id}: {e}")

    async def run(self):
        """Main sync loop"""

        await self.initialize()

        while True:
            try:
                # Get list of users to sync
                if self.auth_method == "admin_basic":
                    # Admin can list all users
                    admin_client = NextcloudClient.from_env()
                    users = await admin_client.users.list_users()
                    user_ids = [u.id for u in users]
                else:
                    # OAuth methods: only sync users with stored tokens
                    user_ids = await self.token_storage.get_all_user_ids()

                logger.info(f"Syncing content for {len(user_ids)} users")

                for user_id in user_ids:
                    await self.sync_user_content(user_id)

                logger.info("Sync complete, sleeping...")
                await asyncio.sleep(300)  # 5 minutes

            except Exception as e:
                logger.error(f"Sync failed: {e}")
                await asyncio.sleep(60)  # Retry after 1 minute
```

### 4. User Request Verification (Dual-Phase Authorization)

```python
@mcp.tool()
@require_scopes("notes:read")
async def nc_notes_semantic_search(
    query: str,
    ctx: Context,
    limit: int = 10
) -> SemanticSearchResponse:
    """Semantic search with permission verification"""

    # Get user's OAuth client (uses their access token from request)
    user_client = get_client(ctx)
    username = user_client.username

    # Phase 1: Vector search (fast, may include false positives)
    embedding = await vector_service.embed(query)
    candidate_results = await qdrant.search(
        collection_name="nextcloud_content",
        query_vector=embedding,
        query_filter={
            "must": [
                {
                    "should": [
                        {"key": "user_id", "match": {"value": username}},
                        {"key": "shared_with", "match": {"any": [username]}}
                    ]
                },
                {"key": "content_type", "match": {"value": "note"}}
            ]
        },
        limit=limit * 2  # Get extra candidates
    )

    # Phase 2: Verify access via Nextcloud API (authoritative)
    verified_results = []
    for candidate in candidate_results:
        note_id = candidate.payload["note_id"]
        try:
            # This uses user's OAuth token - will fail if no access
            note = await user_client.notes.get_note(note_id)
            verified_results.append({
                "note": note,
                "score": candidate.score
            })
            if len(verified_results) >= limit:
                break
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                # User doesn't have access - skip silently
                logger.debug(f"Filtered out note {note_id} for {username}")
                continue
            raise

    return SemanticSearchResponse(results=verified_results)
```

### 5. Security Implementation

#### 5.1 Token Encryption
```python
# Generate encryption key (store securely)
from cryptography.fernet import Fernet

# On first setup
encryption_key = Fernet.generate_key()
# Store in environment or secrets manager
# NEVER commit to source control

# In production
encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY")  # Base64-encoded Fernet key
```

#### 5.2 Token Rotation
```python
async def rotate_refresh_token(user_id: str):
    """Handle refresh token rotation"""

    old_refresh_token = await token_storage.get_refresh_token(user_id)

    # Exchange for new tokens
    response = await exchange_refresh_token(old_refresh_token)

    if "refresh_token" in response:
        # Store new refresh token
        await token_storage.store_refresh_token(
            user_id,
            response["refresh_token"],
            expires_at=response.get("refresh_expires_in")
        )

        # Securely delete old token
        await token_storage.delete_refresh_token(user_id, old_refresh_token)
```

#### 5.3 Audit Logging
```python
async def audit_log(
    event: str,
    user_id: str,
    resource_type: str,
    resource_id: str,
    auth_method: str
):
    """Log sync operations for audit trail"""

    await audit_db.execute(
        "INSERT INTO audit_logs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            int(time.time()),
            event,  # "index_note", "index_file"
            user_id,
            resource_type,
            resource_id,
            auth_method,
            socket.gethostname()
        )
    )
```

### 6. Configuration

#### 6.1 Environment Variables
```bash
# Tier 1: Offline Access
ENABLE_OFFLINE_ACCESS=true
TOKEN_ENCRYPTION_KEY=<base64-encoded-fernet-key>
TOKEN_STORAGE_DB=/app/data/tokens.db

# Tier 2: Token Exchange (auto-detected)
# No configuration needed - detected via OIDC discovery

# Tier 3: Admin Fallback
NEXTCLOUD_USERNAME=sync-bot
NEXTCLOUD_PASSWORD=<secure-password>

# Vector Database
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=<api-key>

# Sync Configuration
SYNC_INTERVAL_SECONDS=300
SYNC_BATCH_SIZE=100
```

#### 6.2 Docker Compose
```yaml
services:
  mcp-sync:
    build: .
    command: ["python", "-m", "nextcloud_mcp_server.sync_worker"]
    environment:
      - NEXTCLOUD_HOST=http://app:80
      - ENABLE_OFFLINE_ACCESS=true
      - TOKEN_ENCRYPTION_KEY=${TOKEN_ENCRYPTION_KEY}
      - QDRANT_URL=http://qdrant:6333
      # OAuth client credentials (for token refresh)
      - NEXTCLOUD_OIDC_CLIENT_ID=${NEXTCLOUD_OIDC_CLIENT_ID}
      - NEXTCLOUD_OIDC_CLIENT_SECRET=${NEXTCLOUD_OIDC_CLIENT_SECRET}
    volumes:
      - sync-tokens:/app/data
    depends_on:
      - app
      - qdrant

volumes:
  sync-tokens:  # Persistent storage for encrypted tokens
```

## Consequences

### Benefits

1. **OAuth-Native Authentication**
   - Leverages standard OAuth flows (offline_access, token exchange)
   - No reliance on admin passwords in production
   - Compatible with enterprise OIDC providers

2. **User-Level Permissions**
   - Each user's content indexed with their own credentials
   - Respects sharing, permissions, and access controls
   - Full audit trail of which user's token was used

3. **Security**
   - Tokens encrypted at rest
   - Short-lived access tokens (refreshed as needed)
   - Token rotation support
   - Defense in depth with dual-phase authorization

4. **Flexibility**
   - Automatic capability detection
   - Graceful degradation through authentication tiers
   - Works with varying OIDC provider capabilities

5. **Operational**
   - Background sync independent of user activity
   - Efficient batch processing
   - Clear separation of sync vs request credentials

### Limitations

1. **Complexity**
   - Multiple authentication paths to maintain
   - Token storage and encryption infrastructure
   - More moving parts than simple admin auth

2. **User Experience**
   - `offline_access` scope may require additional consent
   - Users must authenticate at least once for indexing
   - New users not automatically indexed

3. **OIDC Provider Dependency**
   - Token exchange requires RFC 8693 support (rare)
   - Refresh token rotation varies by provider
   - Some providers may not support offline_access

4. **Operational Overhead**
   - Token database maintenance
   - Monitoring token expiration
   - Handling revoked tokens gracefully

### Security Considerations

#### Threat Model

**Threat 1: Token Storage Breach**
- **Mitigation**: Encryption at rest using Fernet
- **Mitigation**: Secure key management (secrets manager)
- **Mitigation**: Minimal token lifetime
- **Detection**: Audit logs for unusual access patterns

**Threat 2: Token Replay**
- **Mitigation**: Short-lived access tokens (refreshed frequently)
- **Mitigation**: Token rotation on each refresh
- **Mitigation**: Revocation support

**Threat 3: Privilege Escalation**
- **Mitigation**: Dual-phase authorization (vector DB + Nextcloud API)
- **Mitigation**: Sync worker uses same scopes as user requests
- **Mitigation**: Per-user token isolation

**Threat 4: Vector Database Poisoning**
- **Mitigation**: User requests always verify via Nextcloud API
- **Mitigation**: Vector DB is cache/accelerator, not source of truth
- **Mitigation**: Sync operations audited per user

#### Security Best Practices

1. **Token Encryption Key Management**
   ```bash
   # Generate secure key
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

   # Store in secrets manager (Vault, AWS Secrets Manager, etc.)
   # Or use environment variable with restricted permissions
   ```

2. **Token Storage Permissions**
   ```bash
   # Restrict database file permissions
   chmod 600 /app/data/tokens.db
   chown mcp-server:mcp-server /app/data/tokens.db
   ```

3. **Token Rotation Schedule**
   - Refresh access tokens every 5 minutes (or token expiry)
   - Rotate refresh tokens on each use (if provider supports)
   - Revoke tokens on user logout/deauthorization

4. **Monitoring and Alerting**
   - Alert on token refresh failures
   - Monitor for unusual access patterns
   - Track token age and rotation
   - Audit sync operations per user

### Future Enhancements

1. **Token Revocation Handling**
   - Webhook endpoint for token revocation events
   - Periodic validation of stored tokens
   - Graceful handling of revoked tokens

2. **Selective Sync**
   - Allow users to opt-in/opt-out of indexing
   - Per-content-type sync preferences
   - Privacy controls for sensitive content

3. **Multi-Tenant Token Storage**
   - Separate token databases per tenant
   - Key rotation per tenant
   - Tenant isolation

4. **Token Lifecycle Management**
   - Automatic cleanup of expired tokens
   - Token usage analytics
   - Token health dashboard

5. **Alternative OAuth Flows**
   - Device flow for headless sync
   - Resource owner password credentials (ROPC) as fallback
   - SAML assertion grants

## Alternatives Considered

### Alternative 1: Admin BasicAuth Only

**Approach**: Background worker always uses admin credentials

**Pros**:
- Simple implementation
- No token storage complexity
- Works with any authentication backend

**Cons**:
- Violates principle of least privilege
- Single powerful credential
- No per-user audit trail
- Bypasses OAuth entirely

**Decision**: Rejected for production use; kept as fallback only

### Alternative 2: Client Credentials Grant Only

**Approach**: Service account with broad read permissions

**Pros**:
- OAuth-native pattern
- No user token storage
- Standard OAuth flow

**Cons**:
- Requires client_credentials support (may not be available)
- Still needs broad cross-user permissions
- Not well-suited for multi-user indexing

**Decision**: Rejected; token exchange is better fit for multi-user scenario

### Alternative 3: Per-User Access Token Storage

**Approach**: Store user access tokens (not refresh tokens)

**Pros**:
- Simpler than refresh token flow
- No token refresh logic needed

**Cons**:
- Access tokens are short-lived (1-24 hours)
- Requires frequent re-authentication
- Poor user experience
- Sync gaps when tokens expire

**Decision**: Rejected; refresh tokens provide better UX

### Alternative 4: On-Demand Indexing Only

**Approach**: Index content when user searches (no background worker)

**Pros**:
- Uses user's request token
- No background auth needed
- Simpler architecture

**Cons**:
- Very slow first search
- Poor user experience
- Incomplete index
- Can't pre-compute embeddings

**Decision**: Rejected; background indexing is essential for semantic search

### Alternative 5: Nextcloud App Tokens

**Approach**: Generate app-specific passwords for each user

**Pros**:
- Nextcloud-native feature
- User-controlled revocation
- Scoped per-application

**Cons**:
- Requires user interaction to create
- May not support programmatic creation
- Still requires secure storage
- Not standard OAuth

**Decision**: Rejected; not automatable for background worker

## Related Decisions

- ADR-001: Enhanced Note Search (establishes need for vector search)
- [Future] ADR-003: Vector Database Selection
- [Future] ADR-004: Embedding Model Strategy

## References

- [RFC 8693: OAuth 2.0 Token Exchange](https://datatracker.ietf.org/doc/html/rfc8693)
- [RFC 6749: OAuth 2.0 - Refresh Tokens](https://datatracker.ietf.org/doc/html/rfc6749#section-1.5)
- [OpenID Connect Core - Offline Access](https://openid.net/specs/openid-connect-core-1_0.html#OfflineAccess)
- [OWASP: OAuth Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/OAuth2_Cheat_Sheet.html)
- [RFC 8707: Resource Indicators for OAuth 2.0](https://datatracker.ietf.org/doc/html/rfc8707)
