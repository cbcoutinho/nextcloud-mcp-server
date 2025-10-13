# Authentication

The Nextcloud MCP server supports two authentication modes for connecting to your Nextcloud instance.

## Authentication Modes Comparison

| Mode | Status | Security | Use Case |
|------|--------|----------|----------|
| **OAuth2/OIDC** | ✅ Recommended | 🔒 High | Production deployments, multi-user scenarios |
| **Basic Auth** | ⚠️ Legacy | ⚠️ Lower | Development, backward compatibility |

## OAuth2/OIDC (Recommended)

OAuth2/OIDC authentication provides secure, token-based authentication following modern security standards.

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

**Important:** The `user_oidc` app requires a patch for Bearer token support on non-OCS endpoints (like Notes API). See [oauth2-bearer-token-session-issue.md](oauth2-bearer-token-session-issue.md) for details.

### Benefits
- **Zero-config deployment** via dynamic client registration
- **No credential storage** in environment variables
- **Per-user authentication** with access tokens
- **Automatic token validation** via Nextcloud OIDC
- **Secure by design** following OAuth 2.0 standards

### Current Implementation Limitations

> [!IMPORTANT]
> **Tested Configuration:**
> - ✅ Nextcloud `oidc` app (OIDC Identity Provider) + `user_oidc` app (OIDC User Backend)
> - ✅ Nextcloud acting as its own identity provider (self-hosted OIDC)
>
> **Not Tested:**
> - ❌ External identity providers (Azure AD, Keycloak, Okta, etc.)
> - ❌ Using `user_oidc` with external OIDC providers
>
> **Known Requirements:**
> - 🔧 The `user_oidc` app requires a patch for Bearer token support on non-OCS endpoints (see [oauth2-bearer-token-session-issue.md](oauth2-bearer-token-session-issue.md))
> - ⏱️ Dynamic client registration credentials expire (default: 1 hour) - use pre-configured clients for production

### How OAuth Works

When a client connects to the MCP server with OAuth enabled:

1. Client receives OAuth authorization URL from the MCP server
2. User authenticates via browser to Nextcloud
3. Nextcloud redirects back with authorization code
4. Client exchanges code for access token
5. Client uses token to access MCP server

All API requests to Nextcloud use the user's OAuth token, ensuring proper permissions and audit trails.

### See Also
- [OAuth Setup Guide](oauth-setup.md) - Step-by-step setup instructions
- [Configuration](configuration.md) - Environment variables
- [Troubleshooting](troubleshooting.md) - Common OAuth issues

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
