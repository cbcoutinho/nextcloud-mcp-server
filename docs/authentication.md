# Authentication

The Nextcloud MCP server supports two authentication modes for connecting to your Nextcloud instance.

## Authentication Modes Comparison

| Mode | Status | Security | Use Case |
|------|--------|----------|----------|
| **OAuth2/OIDC** | ✅ Recommended | 🔒 High | Production deployments, multi-user scenarios |
| **Basic Auth** | ⚠️ Legacy | ⚠️ Lower | Development, backward compatibility |

## OAuth2/OIDC (Recommended)

OAuth2/OIDC authentication provides secure, token-based authentication following modern security standards.

### Architecture

The Nextcloud MCP Server acts as an **OAuth 2.0 Resource Server**, protecting access to Nextcloud resources:

```
MCP Client ←→ MCP Server (Resource Server) ←→ Nextcloud (Authorization Server + APIs)
            OAuth Flow with PKCE            Bearer Token Auth
```

**Key Components**:
- **MCP Server**: OAuth Resource Server (validates tokens, provides MCP tools)
- **Nextcloud `oidc` app**: OAuth Authorization Server (issues tokens)
- **Nextcloud `user_oidc` app**: Token validation middleware
- **MCP Client**: Any MCP-compatible client (Claude, custom clients)

For detailed architecture, see [OAuth Architecture](oauth-architecture.md).

### Required Nextcloud Apps

OAuth authentication requires **two Nextcloud apps** to work together:

#### 1. `oidc` - OIDC Identity Provider
**Purpose:** Makes Nextcloud an OAuth2/OIDC authorization server

**Provides:**
- OAuth2 authorization endpoint (`/apps/oidc/authorize`)
- Token endpoint (`/apps/oidc/token`)
- User info endpoint (`/apps/oidc/userinfo`)
- JWKS endpoint for token validation (`/apps/oidc/jwks`)
- Dynamic client registration endpoint (`/apps/oidc/register`)

**Installation:** Available in Nextcloud App Store under "Security"

#### 2. `user_oidc` - OpenID Connect User Backend
**Purpose:** Authenticates users and validates Bearer tokens

**Provides:**
- Bearer token validation against the OIDC provider
- User authentication via OIDC
- Session management for authenticated users

**Installation:** Available in Nextcloud App Store under "Security"

**Important:** The `user_oidc` app requires a patch for Bearer token support on non-OCS endpoints (like Notes API). See [Upstream Status](oauth-upstream-status.md) for details.

### Benefits
- **Zero-config deployment** via dynamic client registration
- **No credential storage** in environment variables
- **Per-user authentication** with access tokens
- **Per-user permissions** - each user has their own Nextcloud client
- **Automatic token validation** via Nextcloud OIDC userinfo endpoint
- **Token caching** for performance (default: 1 hour TTL)
- **PKCE required** for enhanced security (S256 code challenge)
- **Secure by design** following OAuth 2.0 and OpenID Connect standards

### Current Implementation Limitations

> [!IMPORTANT]
> **Tested Configuration:**
> - ✅ Nextcloud `oidc` app (OIDC Identity Provider) + `user_oidc` app (OIDC User Backend)
> - ✅ Nextcloud acting as its own identity provider (self-hosted OIDC)
> - ✅ MCP server as OAuth Resource Server
> - ✅ PKCE with S256 code challenge method
>
> **Not Tested:**
> - ❌ External identity providers (Azure AD, Keycloak, Okta, etc.)
> - ❌ Using `user_oidc` with external OIDC providers
>
> **Known Requirements:**
> - 🔧 The `user_oidc` app requires a patch for Bearer token support on non-OCS endpoints (see [Upstream Status](oauth-upstream-status.md))
> - ⏱️ Dynamic client registration credentials expire (default: 1 hour) - use pre-configured clients for production
> - 🔐 PKCE must be advertised in OIDC discovery (see [Upstream Status](oauth-upstream-status.md))

### How OAuth Works

The MCP server implements the OAuth 2.0 Resource Server pattern:

**Phase 1: Authorization (OAuth Flow with PKCE)**
1. MCP client connects and receives OAuth settings (issuer URL, scopes)
2. Client initiates OAuth flow with PKCE (Proof Key for Code Exchange)
3. User authenticates via browser to Nextcloud
4. Nextcloud redirects back with authorization code
5. Client exchanges code + code_verifier for access token

**Phase 2: API Access (Bearer Token Validation)**
6. Client sends MCP requests with `Authorization: Bearer <token>` header
7. MCP server validates token by calling Nextcloud's userinfo endpoint
8. Server creates per-user NextcloudClient instance with the token
9. All Nextcloud API requests use the user's Bearer token
10. User-specific permissions and audit trails apply

This ensures:
- Each user has their own authenticated session
- Actions appear from the correct user in Nextcloud logs
- Proper permission boundaries are maintained
- No shared credentials between users

### See Also
- [OAuth Quick Start](quickstart-oauth.md) - 5-minute setup for development
- [OAuth Setup Guide](oauth-setup.md) - Detailed production setup
- [OAuth Architecture](oauth-architecture.md) - Technical details
- [Upstream Status](oauth-upstream-status.md) - Required patches and PR status
- [OAuth Troubleshooting](oauth-troubleshooting.md) - OAuth-specific issues
- [Configuration](configuration.md) - Environment variables

## Basic Authentication (Legacy)

Basic Authentication uses username and password credentials directly.

### Benefits
- **Simple setup** with username/password
- **Single-user** server instances
- **Quick for development** and testing

### Limitations
- **Credentials in environment** (less secure)
- **Single user only** - all requests use the same account
- **No audit trail** - all actions appear from the same user
- **Maintained for compatibility** - will be deprecated in future versions

> [!WARNING]
> **Security Notice:** Basic Authentication stores credentials in environment variables and is less secure than OAuth. It's maintained for backward compatibility only and may be deprecated in future versions. Use OAuth for production deployments.

### See Also
- [Configuration](configuration.md#basic-authentication-legacy) - BasicAuth environment variables
- [Running the Server](running.md#basicauth-mode-legacy) - BasicAuth examples

## Hybrid Authentication (Multi-User BasicAuth + OAuth)

When running in multi-user BasicAuth mode with `ENABLE_OFFLINE_ACCESS=true`, the server operates in **hybrid authentication mode**. This provides the simplicity of BasicAuth for normal operations with the security of OAuth for administrative functions.

### Authentication Domains

**MCP Operations** (Tools, Resources):
- **Auth Method**: BasicAuth (HTTP Basic username/password)
- **Characteristics**:
  - Stateless - no token storage
  - Simple configuration
  - Direct credential validation against Nextcloud
  - Credentials passed per-request in Authorization header
- **Used For**: MCP tool calls from Claude, MCP client operations

**Management APIs** (Webhooks, Admin UI):
- **Auth Method**: OAuth bearer tokens
- **Characteristics**:
  - Per-user authorization via OAuth consent flow
  - Refresh tokens stored for background operations
  - Token validation via UnifiedTokenVerifier
  - Explicit user consent required
- **Used For**: Astrolabe admin UI, webhook management, vector sync operations

### Configuration

```env
# Enable multi-user BasicAuth
ENABLE_MULTI_USER_BASIC_AUTH=true

# Enable hybrid mode (OAuth provisioning for management APIs)
ENABLE_OFFLINE_ACCESS=true

# Enable background sync (required for hybrid mode currently)
VECTOR_SYNC_ENABLED=true

# Encryption key for refresh token storage
TOKEN_ENCRYPTION_KEY=<base64-encoded-key>

# Nextcloud connection
NEXTCLOUD_HOST=https://cloud.example.com

# OAuth credentials (optional - uses DCR if not set)
NEXTCLOUD_OIDC_CLIENT_ID=<client-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<client-secret>
```

### OAuth Provisioning Flow

1. Admin opens Astrolabe admin settings in Nextcloud
2. Clicks "Authorize" to enable webhook management
3. Redirected to `/oauth/authorize-nextcloud` on MCP server
4. MCP server redirects to Nextcloud OAuth consent page
5. Admin grants OAuth consent (scopes: `openid`, `profile`, `offline_access`)
6. Redirected back to `/oauth/callback` on MCP server
7. MCP server stores refresh token (encrypted)
8. Admin can now manage webhooks from Astrolabe UI

### Benefits

- **Simple MCP client setup**: Use BasicAuth (no OAuth complexity for end users)
- **Secure background operations**: Webhooks use per-user OAuth tokens (no shared credentials)
- **Explicit authorization**: Admins must explicitly grant OAuth consent for webhook operations
- **Per-user isolation**: Each admin's webhook operations use their own refresh token

### Trade-offs

- **Two auth systems**: More complex server configuration than pure BasicAuth or OAuth
- **OAuth setup required**: Admins must complete OAuth flow before managing webhooks
- **Token storage**: Requires database and encryption key for refresh tokens

### Comparison

| Feature | Pure BasicAuth | Hybrid Mode | Pure OAuth |
|---------|---------------|-------------|------------|
| MCP Operations | BasicAuth | BasicAuth | OAuth Bearer Token |
| Management API | N/A | OAuth Bearer Token | OAuth Bearer Token |
| Webhook Operations | N/A | OAuth Refresh Token | OAuth Refresh Token |
| MCP Client Setup | Simple | Simple | Complex (PKCE flow) |
| Admin UI Auth | N/A | OAuth Consent | OAuth Login |
| Token Storage | None | Refresh tokens only | All tokens |
| Deployment Complexity | Low | Medium | High |

### Astrolabe User Setup (Hybrid Mode)

For Astrolabe-specific user setup instructions in hybrid mode, see the [Astrolabe documentation](https://github.com/cbcoutinho/astrolabe/blob/master/docs/user-setup-hybrid-mode.md).

### See Also
- [OAuth Architecture](oauth-architecture.md) - Progressive Consent (Flow 2) details
- [Configuration](configuration.md#enable_offline_access) - Hybrid mode configuration

## Mode Detection

The server automatically detects the authentication mode:

- **OAuth mode**: When `NEXTCLOUD_USERNAME` and `NEXTCLOUD_PASSWORD` are NOT set
- **BasicAuth mode**: When both username and password are provided

You can also force a specific mode using CLI flags:
```bash
# Force OAuth mode
uv run nextcloud-mcp-server --oauth

# Force BasicAuth mode
uv run nextcloud-mcp-server --no-oauth
```

## Switching Between Modes

See [Troubleshooting: Switching Between OAuth and BasicAuth](troubleshooting.md#switching-between-oauth-and-basicauth) for instructions.
