# Authentication

The Nextcloud MCP server supports two authentication modes for connecting to your Nextcloud instance.

## Authentication Modes Comparison

| Mode | Status | Security | Use Case |
|------|--------|----------|----------|
| **OAuth2/OIDC** | ✅ Recommended | 🔒 High | Production deployments, multi-user scenarios |
| **Basic Auth** | ⚠️ Legacy | ⚠️ Lower | Development, backward compatibility |

## OAuth2/OIDC (Recommended)

OAuth2/OIDC authentication provides secure, token-based authentication following modern security standards.

### Benefits
- **Zero-config deployment** via dynamic client registration
- **No credential storage** in environment variables
- **Per-user authentication** with access tokens
- **Automatic token validation** via Nextcloud OIDC
- **Secure by design** following OAuth 2.0 standards

### Current Implementation Limitations

> [!IMPORTANT]
> - Only tested with Nextcloud `user_oidc` and `oidc` apps (Nextcloud as identity provider)
> - Requires a patch for Bearer token support on non-OCS endpoints (see [oauth2-bearer-token-session-issue.md](oauth2-bearer-token-session-issue.md))
> - External identity providers (Azure AD, Keycloak, etc.) have not been tested
> - Dynamic client registration credentials expire (default: 1 hour) - use pre-configured clients for production

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
