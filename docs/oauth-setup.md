# OAuth Setup Guide

This guide walks you through setting up OAuth2/OIDC authentication for the Nextcloud MCP server.

## Prerequisites

- Nextcloud instance with administrator access
- Python 3.11+ installed
- Nextcloud MCP server installed (see [Installation Guide](installation.md))

## Step 1: Install Required Nextcloud Apps

OAuth authentication requires **two apps** to work together:

### Install the OIDC Identity Provider App

1. Open your Nextcloud instance as an administrator
2. Navigate to **Apps** → **Security**
3. Find and install the **OIDC** app (full name: "OIDC Identity Provider")
4. Enable the app

This app makes Nextcloud an OAuth2/OIDC authorization server.

### Install the OpenID Connect User Backend App

1. In **Apps** → **Security**
2. Find and install the **OpenID Connect user backend** app (app ID: `user_oidc`)
3. Enable the app

This app handles Bearer token validation and user authentication.

> [!IMPORTANT]
> **Required Patch:** The `user_oidc` app needs a patch for Bearer token authentication to work with non-OCS endpoints (like Notes API). See [oauth2-bearer-token-session-issue.md](oauth2-bearer-token-session-issue.md) for the patch and installation instructions.

## Step 2: Configure OIDC Apps

### Enable Dynamic Client Registration (for `oidc` app)

1. Navigate to **Settings** → **OIDC** (in Administration settings)
2. Find the **Dynamic Client Registration** section
3. Enable **"Allow dynamic client registration"**
4. (Optional) Configure client expiration time:
   ```bash
   # Via Nextcloud CLI (occ) - optional, default is 3600 seconds (1 hour)
   php occ config:app:set oidc expire_time --value "86400"  # 24 hours
   ```

### Enable Bearer Token Validation (for `user_oidc` app)

Configure the `user_oidc` app to validate bearer tokens from the `oidc` Identity Provider:

```bash
# Via Nextcloud CLI (occ) - required for Bearer token authentication
php occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean
```

This tells the `user_oidc` app to validate Bearer tokens against Nextcloud's own OIDC Identity Provider.

## Step 3: Choose Your Setup Approach

You have two options for configuring OAuth clients:

### Approach A: Automatic Registration (Zero-config)

**Best for:** Development, testing, short-lived deployments

**How it works:** The MCP server automatically registers a new OAuth client with Nextcloud at startup using dynamic client registration.

**Pros:**
- Zero configuration required
- Quick to set up
- No manual client management

**Cons:**
- Clients expire (default: 1 hour)
- Server must re-register on restart if expired
- Not recommended for long-running production deployments

[Jump to Approach A setup →](#approach-a-automatic-registration)

### Approach B: Pre-configured Client (Production)

**Best for:** Production, long-running deployments

**How it works:** You manually create an OAuth client via Nextcloud CLI and provide credentials to the MCP server.

**Pros:**
- Credentials don't expire
- Stable for production use
- More control over client configuration

**Cons:**
- Requires manual setup
- Needs access to Nextcloud server CLI

[Jump to Approach B setup →](#approach-b-pre-configured-client)

---

## Approach A: Automatic Registration

### 1. Configure Environment

Create your `.env` file with only the Nextcloud host:

```dotenv
# .env file
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# Leave these EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

### 2. Start the MCP Server

```bash
# Load environment variables
export $(grep -v '^#' .env | xargs)

# Start server with OAuth enabled
uv run nextcloud-mcp-server --oauth
```

### 3. Verify Registration

The server will automatically register a new OAuth client. Look for these log messages:

```
INFO     OAuth mode detected (NEXTCLOUD_USERNAME/PASSWORD not set)
INFO     Configuring MCP server for OAuth mode
INFO     Performing OIDC discovery: https://your.nextcloud.instance.com/.well-known/openid-configuration
INFO     OIDC discovery successful
INFO     Attempting dynamic client registration...
INFO     Dynamic client registration successful
INFO     OAuth client ready: <client-id>...
INFO     Saved OAuth client credentials to .nextcloud_oauth_client.json
INFO     OAuth initialization complete
```

### 4. Client Credential Storage

Registered client credentials are saved to `.nextcloud_oauth_client.json` by default. The server will:
- Load existing credentials on startup
- Check if they've expired
- Re-register automatically if expired or missing

**Note:** Since dynamically registered clients expire (default: 1 hour), the server checks credentials at startup. For long-running deployments, consider using Approach B (pre-configured clients) instead.

---

## Approach B: Pre-configured Client

### 1. Register Client via Nextcloud CLI

SSH into your Nextcloud server and run:

```bash
# Create OAuth client
php occ oidc:create \
  --name="Nextcloud MCP Server" \
  --type=confidential \
  --redirect-uri="http://localhost:8000/oauth/callback"

# Example output:
# Client ID: abc123xyz
# Client Secret: secret456def
```

**Note:** Adjust the `--redirect-uri` to match your MCP server URL if different from `http://localhost:8000`.

### 2. Configure Environment

Add the client credentials to your `.env` file:

```dotenv
# .env file
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# OAuth Client Credentials
NEXTCLOUD_OIDC_CLIENT_ID=abc123xyz
NEXTCLOUD_OIDC_CLIENT_SECRET=secret456def

# Optional: Custom OAuth configuration
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000
NEXTCLOUD_OIDC_CLIENT_STORAGE=.nextcloud_oauth_client.json

# Leave these EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

See [Configuration Guide](configuration.md#oauth2oidc-configuration) for all available options.

### 3. Start the MCP Server

```bash
# Load environment variables
export $(grep -v '^#' .env | xargs)

# Start server - it will use pre-configured credentials
uv run nextcloud-mcp-server --oauth
```

### 4. Verify Configuration

Look for these log messages:

```
INFO     OAuth mode detected (NEXTCLOUD_USERNAME/PASSWORD not set)
INFO     Configuring MCP server for OAuth mode
INFO     Performing OIDC discovery: https://your.nextcloud.instance.com/.well-known/openid-configuration
INFO     OIDC discovery successful
INFO     Using pre-configured OAuth client: abc123xyz
INFO     OAuth initialization complete
```

**Benefits:** Pre-configured clients don't expire automatically and are more stable for production use.

---

## Step 4: Test Authentication

The MCP server is now configured for OAuth. When clients connect:

1. Client connects to MCP server
2. Server provides OAuth authorization URL
3. User opens URL in browser and authenticates to Nextcloud
4. Nextcloud redirects back with authorization code
5. Client exchanges code for access token
6. Client uses Bearer token to access MCP server
7. All Nextcloud API requests use the user's OAuth token

### Test with MCP Inspector

```bash
# Start MCP Inspector
uv run mcp dev

# In the browser UI:
# 1. Enter your MCP server URL (e.g., http://localhost:8000)
# 2. Complete OAuth flow in browser
# 3. Test tools and resources
```

## Next Steps

- [Running the Server](running.md) - Additional server options
- [Configuration](configuration.md) - All environment variables
- [Troubleshooting](troubleshooting.md) - Common OAuth issues

## See Also

- [Authentication Overview](authentication.md) - OAuth vs BasicAuth comparison
- [OAuth Bearer Token Issue](oauth2-bearer-token-session-issue.md) - Required patch for non-OCS endpoints
