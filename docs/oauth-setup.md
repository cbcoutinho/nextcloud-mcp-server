# OAuth Setup Guide

This guide walks you through setting up OAuth2/OIDC authentication for the Nextcloud MCP server in production.

> **Quick Start?** If you want a 5-minute setup for development, see [OAuth Quick Start](quickstart-oauth.md).

## Table of Contents

- [Prerequisites](#prerequisites)
- [Architecture Overview](#architecture-overview)
- [Step 1: Install Nextcloud Apps](#step-1-install-nextcloud-apps)
- [Step 2: Configure OIDC Apps](#step-2-configure-oidc-apps)
- [Step 3: Choose Deployment Mode](#step-3-choose-deployment-mode)
- [Step 4: Configure MCP Server](#step-4-configure-mcp-server)
- [Step 5: Start and Verify](#step-5-start-and-verify)
- [Testing Authentication](#testing-authentication)
- [Production Recommendations](#production-recommendations)

## Prerequisites

Before beginning, ensure you have:

- **Nextcloud instance** with administrator access
- **Nextcloud version** 28 or later
- **SSH/CLI access** to Nextcloud server (for `occ` commands)
- **Python 3.11+** installed on MCP server host
- **MCP server installed** (see [Installation Guide](installation.md))

## Architecture Overview

The OAuth implementation uses the following components:

```
MCP Client ←→ MCP Server (Resource Server) ←→ Nextcloud (Authorization Server + APIs)
            OAuth Flow                       Bearer Token Auth
```

**Key Roles**:
- **MCP Server**: OAuth Resource Server (validates tokens, provides MCP tools)
- **Nextcloud `oidc` app**: OAuth Authorization Server (issues tokens)
- **Nextcloud `user_oidc` app**: Token validation middleware

For detailed architecture, see [OAuth Architecture](oauth-architecture.md).

## Step 1: Install Nextcloud Apps

OAuth authentication requires **two Nextcloud apps** to work together.

### Required Apps

#### 1. `oidc` - OIDC Identity Provider

**Purpose**: Makes Nextcloud an OAuth2/OIDC authorization server

**Installation**:
1. Open Nextcloud as administrator
2. Navigate to **Apps** → **Security**
3. Find **"OIDC"** (full name: "OIDC Identity Provider")
4. Click **Enable** or **Download and enable**

**Provides**:
- OAuth2 authorization endpoint
- Token endpoint
- User info endpoint
- JWKS endpoint
- Dynamic client registration endpoint (optional)

#### 2. `user_oidc` - OpenID Connect User Backend

**Purpose**: Authenticates users and validates Bearer tokens

**Installation**:
1. In **Apps** → **Security**
2. Find **"OpenID Connect user backend"** (app ID: `user_oidc`)
3. Click **Enable** or **Download and enable**

**Provides**:
- Bearer token validation against OIDC provider
- User authentication via OIDC
- Session management for authenticated users

> [!IMPORTANT]
> **Upstream Patch Required**: The `user_oidc` app needs a patch for Bearer token support with app-specific APIs (Notes, Calendar, etc.). The patch is pending upstream review.
>
> **Status**: See [Upstream Status](oauth-upstream-status.md) for current PR status and workarounds.
>
> **Impact**: OCS APIs work without patch, but app-specific APIs require the patch.

### Verify Installation

```bash
# Check both apps are installed and enabled
php occ app:list | grep -E "oidc|user_oidc"

# Expected output:
#  - oidc: enabled
#  - user_oidc: enabled
```

## Step 2: Configure OIDC Apps

### Configure `oidc` App (Identity Provider)

#### Option A: Dynamic Client Registration (Development)

**Best for**: Development, testing, auto-registration

1. Navigate to **Settings** → **OIDC** (Administration settings)
2. Enable **"Allow dynamic client registration"**
3. (Optional) Configure client expiration:
   ```bash
   # Default: 3600 seconds (1 hour)
   php occ config:app:set oidc expire_time --value "86400"  # 24 hours
   ```

#### Option B: Pre-configured Clients (Production)

**Best for**: Production, long-running deployments

Skip the dynamic registration setting. You'll manually register clients via CLI in Step 3.

### Configure `user_oidc` App (Token Validation)

**Required**: Enable Bearer token validation:

```bash
# SSH into Nextcloud server
php occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean
```

This tells `user_oidc` to validate Bearer tokens against Nextcloud's OIDC Identity Provider.

### Verify OIDC Discovery

Test that OIDC discovery endpoint is accessible:

```bash
curl https://your.nextcloud.instance.com/.well-known/openid-configuration | jq
```

Expected response:
```json
{
  "issuer": "https://your.nextcloud.instance.com",
  "authorization_endpoint": "https://your.nextcloud.instance.com/apps/oidc/authorize",
  "token_endpoint": "https://your.nextcloud.instance.com/apps/oidc/token",
  "userinfo_endpoint": "https://your.nextcloud.instance.com/apps/oidc/userinfo",
  "jwks_uri": "https://your.nextcloud.instance.com/apps/oidc/jwks",
  "registration_endpoint": "https://your.nextcloud.instance.com/apps/oidc/register",
  ...
}
```

### PKCE Support

The MCP server **requires PKCE** (Proof Key for Code Exchange) with S256 code challenge method.

**Validation**: The MCP server automatically validates PKCE support at startup by checking the discovery response for `code_challenge_methods_supported`.

**Note**: If PKCE is not advertised in discovery metadata, the server logs a warning but continues (PKCE still works, it's just not advertised). See [Upstream Status](oauth-upstream-status.md) for tracking.

## Step 3: Choose Deployment Mode

You have two options for managing OAuth clients:

### Mode A: Automatic Registration (Dynamic Client Registration)

**Best for**: Development, testing, quick deployments

**How it works**:
- MCP server automatically registers an OAuth client on first startup
- Uses Nextcloud's dynamic client registration endpoint
- Saves credentials to SQLite database
- Reuses stored credentials on subsequent restarts
- Re-registers automatically if credentials expire

**Pros**:
- Zero configuration required
- Quick setup
- Automatic credential management

**Cons**:
- Clients expire (default: 1 hour, configurable)
- Must have dynamic client registration enabled on Nextcloud

**Configuration**: Skip to [Step 4](#step-4-configure-mcp-server) with minimal config.

---

### Mode B: Pre-configured Client (Production)

**Best for**: Production, long-running deployments, stable environments

**How it works**:
- You manually register an OAuth client via Nextcloud CLI
- Provide client credentials to MCP server via environment variables
- Credentials don't expire

**Pros**:
- Credentials don't expire
- Stable for production
- More control over client configuration
- Better for audit trails

**Cons**:
- Requires manual setup
- Needs SSH/CLI access to Nextcloud server

**Setup**: Register a client via CLI:

```bash
# SSH into Nextcloud server
php occ oidc:create \
  --name="Nextcloud MCP Server" \
  --type=confidential \
  --redirect-uri="http://localhost:8000/oauth/callback"

# Example output:
# Client ID: abc123xyz789
# Client Secret: secret456def012

# Save these credentials for Step 4
```

**Important**: Adjust `--redirect-uri` to match your MCP server URL:
- Local: `http://localhost:8000/oauth/callback`
- Remote: `http://your-server:8000/oauth/callback`
- Custom port: `http://your-server:PORT/oauth/callback`

The redirect URI **must** be:
```
{NEXTCLOUD_MCP_SERVER_URL}/oauth/callback
```

## Step 4: Configure MCP Server

Create or update your `.env` file with OAuth configuration.

### For Mode A (Automatic Registration)

```bash
# Copy sample if needed
cp env.sample .env

# Edit .env
cat > .env << 'EOF'
# Nextcloud Instance
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# Leave EMPTY for OAuth mode (do not set USERNAME/PASSWORD)
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=

# Optional: MCP server URL (for OAuth callbacks)
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000
EOF
```

### For Mode B (Pre-configured Client)

```bash
# Copy sample if needed
cp env.sample .env

# Edit .env
cat > .env << 'EOF'
# Nextcloud Instance
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# OAuth Client Credentials (from Step 3)
NEXTCLOUD_OIDC_CLIENT_ID=abc123xyz789
NEXTCLOUD_OIDC_CLIENT_SECRET=secret456def012

# MCP server URL (must match redirect URI)
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000

# Leave EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
EOF
```

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEXTCLOUD_HOST` | ✅ Yes | - | Full URL of Nextcloud instance |
| `NEXTCLOUD_OIDC_CLIENT_ID` | ⚠️ Mode B only | - | OAuth client ID |
| `NEXTCLOUD_OIDC_CLIENT_SECRET` | ⚠️ Mode B only | - | OAuth client secret |
| `NEXTCLOUD_MCP_SERVER_URL` | ⚠️ Optional | `http://localhost:8000` | MCP server URL for callbacks |
| `NEXTCLOUD_USERNAME` | ❌ Must be empty | - | Leave empty for OAuth |
| `NEXTCLOUD_PASSWORD` | ❌ Must be empty | - | Leave empty for OAuth |

See [Configuration Guide](configuration.md) for all options.

## Step 5: Start and Verify

### Load Environment Variables

```bash
# Load from .env file
export $(grep -v '^#' .env | xargs)

# Verify key variables are set
echo "NEXTCLOUD_HOST: $NEXTCLOUD_HOST"
echo "NEXTCLOUD_MCP_SERVER_URL: $NEXTCLOUD_MCP_SERVER_URL"
```

### Start MCP Server

```bash
# Start with OAuth mode
uv run nextcloud-mcp-server --oauth

# Or with custom options
uv run nextcloud-mcp-server --oauth --port 8000 --log-level info
```

### Verify Startup

Look for these success messages:

**For Mode A (Auto-registration)**:
```
INFO     OAuth mode detected (NEXTCLOUD_USERNAME/PASSWORD not set)
INFO     Configuring MCP server for OAuth mode
INFO     Performing OIDC discovery: https://your.nextcloud.instance.com/.well-known/openid-configuration
✓ PKCE support validated: ['S256']
INFO     OIDC discovery successful
INFO     Attempting dynamic client registration...
INFO     Dynamic client registration successful
INFO     OAuth client ready: <client-id>...
INFO     Saved OAuth client credentials to SQLite database
INFO     OAuth initialization complete
INFO     MCP server ready at http://127.0.0.1:8000
```

**For Mode B (Pre-configured)**:
```
INFO     OAuth mode detected (NEXTCLOUD_USERNAME/PASSWORD not set)
INFO     Configuring MCP server for OAuth mode
INFO     Performing OIDC discovery: https://your.nextcloud.instance.com/.well-known/openid-configuration
✓ PKCE support validated: ['S256']
INFO     OIDC discovery successful
INFO     Using pre-configured OAuth client: abc123xyz789
INFO     OAuth initialization complete
INFO     MCP server ready at http://127.0.0.1:8000
```

### Common Startup Issues

| Issue | Solution |
|-------|----------|
| "OAuth mode requires NEXTCLOUD_HOST" | Set `NEXTCLOUD_HOST` in `.env` |
| "OIDC discovery failed" | Verify Nextcloud URL and network connectivity |
| "Dynamic registration failed" | Enable dynamic registration in OIDC app settings |
| "PKCE validation failed" | See [Upstream Status](oauth-upstream-status.md) |

See [OAuth Troubleshooting](oauth-troubleshooting.md) for detailed solutions.

## Testing Authentication

### Test with MCP Inspector

The MCP Inspector provides a web UI for testing:

```bash
# In a new terminal
uv run mcp dev

# Opens browser at http://localhost:6272
```

In the MCP Inspector UI:
1. Enter server URL: `http://localhost:8000/mcp`
2. Click **Connect**
3. Complete OAuth flow in browser popup:
   - Login to Nextcloud
   - Authorize MCP server access
   - Redirected back to MCP Inspector
4. Test tools:
   - Try `nc_notes_create_note`
   - Try `nc_notes_search_notes`
   - Try `nc_calendar_list_events`

### Test from Command Line

```bash
# Get an OAuth token (you'll need to implement client flow or extract from browser)
TOKEN="your_access_token_here"

# Test OCS API (should work)
curl -H "Authorization: Bearer $TOKEN" \
  "$NEXTCLOUD_HOST/ocs/v2.php/cloud/capabilities?format=json" \
  -H "OCS-APIRequest: true"

# Test Notes API (requires upstream patch)
curl -H "Authorization: Bearer $TOKEN" \
  "$NEXTCLOUD_HOST/apps/notes/api/v1/notes"
```

### Verify Token Validation

Check MCP server logs for token validation:

```bash
# Start server with debug logging
uv run nextcloud-mcp-server --oauth --log-level debug

# Look for:
# DEBUG    Token validation via userinfo endpoint
# DEBUG    Token validated successfully for user: username
```

## Production Recommendations

### Security Best Practices

1. **Use Pre-configured Clients** (Mode B)
   - More stable
   - Better audit trails
   - No expiration issues

2. **Secure Credential Storage**
   ```bash
   # Set restrictive permissions on environment file
   chmod 600 .env
   # Database permissions are handled automatically
   ```

3. **Use HTTPS for MCP Server**
   - Especially important for remote access
   - Use reverse proxy (nginx, Apache) with SSL

4. **Restrict Redirect URIs**
   - Only register necessary redirect URIs
   - Use specific URLs (not wildcards)

### Deployment Considerations

1. **MCP Server URL**
   - Must be accessible to OAuth clients
   - Must match redirect URI registered with Nextcloud
   - For Docker: expose port and use correct host

2. **Network Configuration**
   - MCP server must reach Nextcloud (OIDC endpoints)
   - OAuth clients must reach MCP server (callbacks)
   - OAuth clients must reach Nextcloud (authorization flow)

3. **Process Management**
   - Use systemd, supervisord, or Docker for MCP server
   - Ensure automatic restart on failure
   - Monitor logs for OAuth errors

### Example Production Configs

#### Docker Compose

```yaml
version: '3'
services:
  nextcloud-mcp:
    image: ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      NEXTCLOUD_HOST: https://your.nextcloud.instance.com
      NEXTCLOUD_OIDC_CLIENT_ID: ${NEXTCLOUD_OIDC_CLIENT_ID}
      NEXTCLOUD_OIDC_CLIENT_SECRET: ${NEXTCLOUD_OIDC_CLIENT_SECRET}
      NEXTCLOUD_MCP_SERVER_URL: http://your-server:8000
    volumes:
      - ./data:/app/data  # For SQLite database persistence
    command: ["--oauth", "--transport", "streamable-http"]
    restart: unless-stopped
```

#### Systemd Service

```ini
[Unit]
Description=Nextcloud MCP Server (OAuth)
After=network.target

[Service]
Type=simple
User=mcp
WorkingDirectory=/opt/nextcloud-mcp-server
Environment="NEXTCLOUD_HOST=https://your.nextcloud.instance.com"
Environment="NEXTCLOUD_OIDC_CLIENT_ID=abc123xyz789"
Environment="NEXTCLOUD_OIDC_CLIENT_SECRET=secret456def012"
Environment="NEXTCLOUD_MCP_SERVER_URL=http://your-server:8000"
ExecStart=/opt/nextcloud-mcp-server/.venv/bin/nextcloud-mcp-server --oauth
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Monitoring and Maintenance

1. **Log Monitoring**
   ```bash
   # Watch for OAuth errors
   tail -f /var/log/nextcloud-mcp/server.log | grep -i "oauth\|token"
   ```

2. **Token Expiration** (Mode A only)
   - Monitor for "Stored client has expired" messages
   - Consider increasing expiration or switching to Mode B

3. **Upstream Patches**
   - Subscribe to [Upstream Status](oauth-upstream-status.md)
   - Plan to update when patches are merged

## Troubleshooting

For OAuth-specific issues, see [OAuth Troubleshooting](oauth-troubleshooting.md).

Common issues:
- [OIDC discovery failed](oauth-troubleshooting.md#oidc-discovery-failed)
- [Bearer token auth fails](oauth-troubleshooting.md#bearer-token-authentication-fails)
- [Client expired](oauth-troubleshooting.md#client-expired)
- [PKCE errors](oauth-troubleshooting.md#pkce-not-advertised)

## Next Steps

- [OAuth Architecture](oauth-architecture.md) - Understand how OAuth works
- [OAuth Troubleshooting](oauth-troubleshooting.md) - Solve common issues
- [Upstream Status](oauth-upstream-status.md) - Track required patches
- [Configuration](configuration.md) - All environment variables
- [Running the Server](running.md) - Additional server options

## See Also

- [Authentication Overview](authentication.md) - OAuth vs BasicAuth comparison
- [Quick Start Guide](quickstart-oauth.md) - 5-minute setup for development
- [MCP Specification](https://spec.modelcontextprotocol.io/) - MCP protocol details
- [RFC 6749](https://www.rfc-editor.org/rfc/rfc6749) - OAuth 2.0 Framework
- [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636) - PKCE Extension
