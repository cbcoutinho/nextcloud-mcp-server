# OAuth Quick Start Guide

Get up and running with OAuth authentication in 5 minutes.

## Prerequisites Checklist

Before you begin, ensure you have:

- [ ] Nextcloud instance with **administrator access**
- [ ] Nextcloud version 28 or later
- [ ] Python 3.11+ installed
- [ ] `uv` package manager installed ([installation instructions](https://docs.astral.sh/uv/getting-started/installation/))

## Step 1: Install Nextcloud Apps

Install **both** required apps in your Nextcloud instance:

1. Open Nextcloud as administrator
2. Navigate to **Apps** → **Security**
3. Install:
   - **OIDC** (OIDC Identity Provider app)
   - **OpenID Connect user backend** (user_oidc app)
4. Enable both apps

> [!IMPORTANT]
> The `user_oidc` app requires an upstream patch for Bearer token support. See [Upstream Status](oauth-upstream-status.md) for details. The functionality works, but the PR is pending.

## Step 2: Configure Nextcloud OIDC

Enable dynamic client registration and Bearer token validation:

### Via Web UI

1. Go to **Settings** → **OIDC** (Administration settings)
2. Enable **"Allow dynamic client registration"**

### Via CLI (Required)

SSH into your Nextcloud server and run:

```bash
# Enable Bearer token validation
php occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean
```

## Step 3: Install MCP Server

Clone and install the MCP server:

```bash
# Clone repository
git clone https://github.com/cbcoutinho/nextcloud-mcp-server.git
cd nextcloud-mcp-server

# Install dependencies
uv sync
```

## Step 4: Configure Environment

Create a `.env` file with minimal configuration:

```bash
# Copy sample
cp env.sample .env

# Edit .env and set:
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# IMPORTANT: Leave these EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

## Step 5: Start the Server

Load environment variables and start the server:

```bash
# Load environment
export $(grep -v '^#' .env | xargs)

# Start server with OAuth
uv run nextcloud-mcp-server --oauth
```

Look for this success message:

```
✓ PKCE support validated: ['S256']
INFO     OAuth initialization complete
INFO     MCP server ready at http://127.0.0.1:8000
```

## Step 6: Test with MCP Inspector

Open a new terminal and test the connection:

```bash
# Start MCP Inspector
uv run mcp dev
```

This opens your browser. In the MCP Inspector UI:

1. Enter server URL: `http://127.0.0.1:8000/mcp`
2. Click **Connect**
3. Complete the OAuth flow in the browser popup
4. After authorization, you'll see available tools and resources

Test a tool by trying:
- **Tool**: `nc_notes_create_note`
- **Title**: "Test Note"
- **Content**: "Hello from MCP!"
- **Category**: "Notes"

## Troubleshooting Quick Fixes

### PKCE Error

If you see:
```
ERROR: OIDC CONFIGURATION ERROR - Missing PKCE Support Advertisement
```

**Fix**: The Nextcloud OIDC app needs to be updated to advertise PKCE support. See [Upstream Status](oauth-upstream-status.md) for the required PR.

### 401 Unauthorized for Notes API

If OAuth works but Notes API returns 401:

**Fix**: The `user_oidc` app needs the Bearer token patch. See [Upstream Status](oauth-upstream-status.md) for details.

### Can't Reach OIDC Discovery Endpoint

**Fix**: Verify your Nextcloud URL is correct and accessible:

```bash
curl https://your.nextcloud.instance.com/.well-known/openid-configuration
```

## Next Steps

- [OAuth Setup Guide](oauth-setup.md) - Detailed configuration options
- [OAuth Architecture](oauth-architecture.md) - How it works under the hood
- [OAuth Troubleshooting](oauth-troubleshooting.md) - Common issues and solutions
- [Configuration](configuration.md) - All environment variables

## Development vs Production

This quick start uses **automatic client registration** which is perfect for:
- Development
- Testing
- Short-lived deployments

For **production deployments**, you should:
1. Pre-register OAuth clients manually
2. Use dedicated client credentials
3. See [OAuth Setup Guide](oauth-setup.md) for production configuration

---

**Need help?** Check [OAuth Troubleshooting](oauth-troubleshooting.md) or [open an issue](https://github.com/cbcoutinho/nextcloud-mcp-server/issues).
