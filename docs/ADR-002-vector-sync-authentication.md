# ADR-002: Vector Database Background Sync Authentication

## Status
Accepted - Tier 2 (Token Exchange with Delegation) Implemented

**Important**: Service account tokens (old Tier 1) have been rejected as they violate OAuth "act on-behalf-of" principles by creating Nextcloud user accounts for the MCP server.

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

We will implement a **tiered OAuth authentication strategy** for background operations in OAuth mode. When OAuth authentication is not configured or available, the background sync feature is not available.

**Note**: This ADR applies only to **OAuth mode**. In BasicAuth mode (single-user deployments), credentials are already available via environment variables, and background operations work without additional configuration.

### OAuth "Act On-Behalf-Of" Principle

**Core Requirement**: The MCP server must NEVER create its own user identity in Nextcloud when operating in OAuth mode.

**Valid Patterns**:
- ✅ **Foreground operations**: Use user's access token from MCP request (currently implemented)
- ✅ **Background operations**: Token exchange to impersonate/delegate as user (requires provider support)
- ❌ **Service account**: Creates independent identity in Nextcloud (violates OAuth principles)

**Why This Matters**:
1. **Audit Trail**: All operations must be attributable to the actual user, not a service account
2. **Stateless Server**: MCP server should not have persistent identity/state in Nextcloud
3. **Security Model**: Avoid creating "admin by another name" with broad cross-user permissions
4. **OAuth Design**: OAuth tokens represent user authorization, not server authorization

**If Token Exchange Not Available**:
- Background operations simply cannot happen in OAuth mode
- This is correct behavior - not a limitation to work around
- Don't create service accounts as "workaround" - this defeats OAuth's purpose
- Use BasicAuth mode if background operations are critical to your deployment

### Tier 1: Token Exchange with Impersonation (RFC 8693) ⚠️ **NOT IMPLEMENTED**

**Better Security** - Requires provider support for user impersonation

- Service account exchanges token to impersonate specific users
- Each background operation runs as the target user
- Uses `requested_subject` parameter in token exchange
- Per-user permission enforcement at API level

**Requirements**:
- OIDC provider supports RFC 8693 token exchange
- Provider supports user impersonation (rare - requires Legacy Keycloak V1 with preview features)
- Service account has impersonation permissions

**Status**: ⚠️ Not implemented - Keycloak Standard V2 doesn't support impersonation
**Reference**: See `docs/oauth-impersonation-findings.md` for investigation details

### Tier 2: Token Exchange with Delegation (RFC 8693) ✅ **IMPLEMENTED**

**Best Security** - Requires provider support for delegation with `act` claim

- Service account exchanges token on behalf of users (delegation, not impersonation)
- Token includes `act` claim showing service account as actor
- API sees both the user (`sub`) and actor (`act`) in token
- Full audit trail of delegated operations
- **Implementation**: `KeycloakOAuthClient.exchange_token_for_user()` (keycloak_oauth.py:397-495)
- **Testing**: Manual test in `tests/manual/test_token_exchange.py`
- **Limitation**: Keycloak doesn't support `act` claim yet - [Issue #38279](https://github.com/keycloak/keycloak/issues/38279)

**Requirements**:
- OIDC provider supports RFC 8693 token exchange
- Provider supports delegation with `act` claim (very rare)
- Proper token exchange permissions configured

**Current Implementation**: Internal-to-internal token exchange with audience modification (without `act` claim)

### ❌ Will Not Implement

**1. Service Account with Independent Identity (client_credentials)**
- **Status**: Previously proposed as Tier 1, now rejected
- **Why Invalid**: Creates Nextcloud user account for MCP server (e.g., `service-account-nextcloud-mcp-server`)
- **Problems**:
  - **Violates OAuth "act on-behalf-of" principle**: Actions attributed to service account instead of real user
  - **Breaks audit trail**: Can't determine which user initiated the action
  - **Creates stateful server identity**: MCP server has persistent identity/data in Nextcloud
  - **Security risk**: Service account becomes "admin by another name" with broad cross-user permissions
  - **User provisioning side effect**: Nextcloud's `user_oidc` app auto-provisions service account as real user
- **Code Status**: Implementation exists (`KeycloakOAuthClient.get_service_account_token()`) but marked with warnings
- **Alternative**: If service account pattern truly needed, use BasicAuth mode instead of OAuth mode
- **Reference**: See commit c12df98 for detailed analysis of why this approach was rejected

**2. Offline Access with Refresh Tokens**
- **MCP Protocol Architecture**: FastMCP SDK manages OAuth where MCP Client handles refresh tokens
- **Security Model**: Refresh tokens must never be shared between client and server (OAuth best practice)
- **Technical Impossibility**: MCP Server has no access to refresh tokens from the OAuth callback
- **Alternative**: Token exchange provides similar benefits without violating OAuth security model

**3. Admin Credentials Fallback**
- **Out of Scope**: This ADR focuses on OAuth mode only
- **Not Appropriate**: Admin credentials bypass OAuth security model
- **BasicAuth Mode**: For single-user deployments needing background operations, use BasicAuth mode instead

### Key Architectural Principles

1. **Capability Detection**: Automatically detect which OAuth methods are supported
2. **Dual-Phase Authorization**:
   - Sync worker indexes with service credentials
   - User requests verify access with user's OAuth token
3. **Defense in Depth**: Vector database is search accelerator, not security boundary
4. **Separation of Concerns**: Sync credentials ≠ Request credentials

## Implementation Details

### 1. Token Exchange with Impersonation (Tier 1) ⚠️ NOT IMPLEMENTED

This tier is documented for completeness but is not currently implemented due to lack of provider support.

#### 1.1 Impersonation Flow (Conceptual)

```python
async def exchange_for_impersonated_user_token(
    service_token: str,
    target_user_id: str,
    scopes: list[str]
) -> str:
    """Exchange service token to impersonate specific user (NOT IMPLEMENTED)"""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": service_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_subject": target_user_id,  # Impersonate this user
                "audience": "nextcloud",
                "scope": " ".join(scopes)
            },
            auth=(client_id, client_secret)
        )

        response.raise_for_status()
        return response.json()["access_token"]
```

**Why Not Implemented**:
- Keycloak Standard V2 doesn't support `requested_subject` parameter
- Requires Legacy Keycloak V1 with preview features (not production-ready)
- Very few OIDC providers support user impersonation via token exchange

**See**: `docs/oauth-impersonation-findings.md` for detailed investigation

### 2. Token Exchange with Delegation (Tier 2) ✅ IMPLEMENTED

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

#### 2.2 Delegation Token Exchange
```python
async def exchange_for_user_token(
    service_token: str,
    target_user_id: str,
    audience: str,
    scopes: list[str]
) -> str:
    """Exchange service token for user-scoped token via RFC 8693"""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": service_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "audience": audience,  # Target resource server (e.g., "nextcloud")
                "scope": " ".join(scopes)
            },
            auth=(client_id, client_secret)
        )

        if response.status_code != 200:
            logger.warning(f"Token exchange failed: {response.status_code}")
            raise TokenExchangeNotSupportedError()

        return response.json()["access_token"]
```

**Implementation**: `KeycloakOAuthClient.exchange_token_for_user()` (keycloak_oauth.py:397-495)

**Note**: Full delegation with `act` claim requires provider support that is currently very rare. Keycloak tracking: [Issue #38279](https://github.com/keycloak/keycloak/issues/38279)

### 4. Sync Worker with Tiered Authentication

```python
# nextcloud_mcp_server/sync_worker.py
class VectorSyncWorker:
    """Background worker for indexing content into vector database"""

    def __init__(self):
        self.auth_method = None
        self.oauth_client = None  # KeycloakOAuthClient or similar
        self.vector_service = None

    async def initialize(self):
        """Detect and configure authentication method"""

        from nextcloud_mcp_server.auth.keycloak_oauth import KeycloakOAuthClient

        try:
            self.oauth_client = KeycloakOAuthClient.from_env()
            await self.oauth_client.discover()

            # Verify service account access (Tier 1)
            service_token = await self.oauth_client.get_service_account_token()
            logger.info("✓ Service account token acquired")

            # Check if token exchange is supported (Tier 2/3)
            if await check_token_exchange_support(self.oauth_client.discovery_url):
                self.auth_method = "token_exchange_delegation"
                logger.info(
                    "✓ Token exchange supported (RFC 8693) - will use delegation for user-scoped operations"
                )
            else:
                self.auth_method = "service_account"
                logger.info(
                    "ℹ Token exchange not supported - using service account token for all operations"
                )

        except Exception as e:
            logger.error(f"Failed to initialize OAuth authentication: {e}")
            raise RuntimeError(
                "OAuth authentication is required for background sync. "
                "Either configure OIDC_CLIENT_ID/OIDC_CLIENT_SECRET with service account enabled, "
                "or use BasicAuth mode for single-user deployments."
            ) from e

    async def get_user_client(self, user_id: str) -> NextcloudClient:
        """Get authenticated client for user based on auth method"""

        if self.auth_method == "token_exchange_delegation":
            # Tier 2/3: Get service token and exchange for user-scoped token
            service_token_data = await self.oauth_client.get_service_account_token()

            user_token_data = await self.oauth_client.exchange_token_for_user(
                subject_token=service_token_data["access_token"],
                target_user_id=user_id,
                audience="nextcloud",
                scopes=["notes:read", "files:read", "calendar:read"]
            )

            return NextcloudClient.from_token(
                base_url=nextcloud_host,
                token=user_token_data["access_token"],
                username=user_id
            )

        elif self.auth_method == "service_account":
            # Tier 1: Use service account token directly (no user scoping)
            service_token_data = await self.oauth_client.get_service_account_token()

            return NextcloudClient.from_token(
                base_url=nextcloud_host,
                token=service_token_data["access_token"],
                username="service-account"
            )

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
                # Implementation depends on how you track authenticated users
                # Options:
                # - Audit logs of MCP authentication events
                # - MCP session history
                # - Configured user list
                # - If using service account with broad permissions: list all users
                user_ids = await self.get_active_users()

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

#### 5.1 Service Account Credentials Protection
```python
# Store OAuth client credentials securely
# NEVER commit to source control

# Option 1: Environment variables (for development)
export OIDC_CLIENT_ID="nextcloud-mcp-server"
export OIDC_CLIENT_SECRET="<secure-secret>"

# Option 2: Secrets manager (for production)
import boto3
secrets = boto3.client('secretsmanager')
secret = secrets.get_secret_value(SecretId='nextcloud-mcp-oauth')
client_secret = json.loads(secret['SecretString'])['client_secret']

# Option 3: Encrypted storage (for self-hosted)
from nextcloud_mcp_server.auth.refresh_token_storage import RefreshTokenStorage

storage = RefreshTokenStorage.from_env()
await storage.initialize()

# Client credentials are encrypted at rest using Fernet
client_data = await storage.get_oauth_client()
```

#### 5.2 Token Lifecycle Management
```python
async def manage_service_token_lifecycle():
    """Cache and refresh service account tokens"""

    # Cache service token (avoid repeated requests)
    cached_token = None
    token_expires_at = 0

    async def get_fresh_service_token() -> str:
        nonlocal cached_token, token_expires_at

        now = time.time()

        # Return cached token if still valid (with 5-minute buffer)
        if cached_token and now < (token_expires_at - 300):
            return cached_token

        # Request new token
        token_data = await oauth_client.get_service_account_token()

        cached_token = token_data["access_token"]
        token_expires_at = now + token_data.get("expires_in", 3600)

        logger.info("Service account token refreshed")
        return cached_token

    return get_fresh_service_token
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
# OAuth Configuration (Required for Background Sync in OAuth Mode)
# Requires external OIDC provider with client_credentials support
OIDC_DISCOVERY_URL=http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration
OIDC_CLIENT_ID=nextcloud-mcp-server
OIDC_CLIENT_SECRET=<secure-secret>
NEXTCLOUD_HOST=http://app:80

# Tier selection is automatic:
# - Tier 1 (service_account): Always available if client has service account enabled
# - Tier 2/3 (token_exchange): Used if provider supports RFC 8693 token exchange

# Vector Database
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=<api-key>

# Sync Configuration
SYNC_INTERVAL_SECONDS=300
SYNC_BATCH_SIZE=100

# Note: For BasicAuth mode (single-user), background sync uses NEXTCLOUD_USERNAME/NEXTCLOUD_PASSWORD
# This ADR focuses on OAuth mode only
```

#### 6.2 Keycloak Configuration (for Token Exchange)

**Client Settings** (`nextcloud-mcp-server`):
```json
{
  "clientId": "nextcloud-mcp-server",
  "serviceAccountsEnabled": true,
  "authorizationServicesEnabled": false,
  "attributes": {
    "token.exchange.grant.enabled": "true",
    "client.token.exchange.standard.enabled": "true"
  }
}
```

**Service Account Roles**:
- Assign appropriate Nextcloud roles/scopes to the service account
- Configure token exchange permissions

#### 6.3 Docker Compose
```yaml
services:
  mcp-sync:
    build: .
    command: ["python", "-m", "nextcloud_mcp_server.sync_worker"]
    environment:
      - NEXTCLOUD_HOST=http://app:80

      # External OIDC provider (Keycloak)
      - OIDC_DISCOVERY_URL=http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration
      - OIDC_CLIENT_ID=nextcloud-mcp-server
      - OIDC_CLIENT_SECRET=${OIDC_CLIENT_SECRET}

      # Vector database
      - QDRANT_URL=http://qdrant:6333
      - QDRANT_API_KEY=${QDRANT_API_KEY}
    volumes:
      - sync-data:/app/data  # For OAuth client credential storage
    depends_on:
      - app
      - keycloak
      - qdrant

volumes:
  sync-data:  # Persistent storage for encrypted OAuth client credentials
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

1. **OAuth Client Secret Management**
   ```bash
   # Store in secrets manager (Vault, AWS Secrets Manager, etc.)
   # Or use environment variable with restricted permissions

   # For self-hosted: Use encrypted storage
   # OAuth client credentials stored in SQLite with Fernet encryption
   # Encryption key: TOKEN_ENCRYPTION_KEY environment variable

   # Generate encryption key:
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. **Service Account Token Lifecycle**
   - Cache service tokens to minimize requests (with expiry buffer)
   - Automatically refresh expired tokens
   - Use short-lived tokens (provider default, typically 1 hour)
   - Monitor token request rates and failures

3. **Database Permissions (for Client Credential Storage)**
   ```bash
   # Restrict database file permissions
   chmod 600 /app/data/tokens.db
   chown mcp-server:mcp-server /app/data/tokens.db
   ```

4. **Monitoring and Alerting**
   - Alert on token exchange failures
   - Monitor for unusual access patterns
   - Track service account token usage
   - Audit sync operations per user (if delegation supported)

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
