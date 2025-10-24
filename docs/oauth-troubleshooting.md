# OAuth Troubleshooting

This guide covers OAuth-specific issues and solutions for the Nextcloud MCP server.

For general troubleshooting, see [Troubleshooting Guide](troubleshooting.md).

## Quick Diagnosis

Start here to identify your issue:

| Symptom | Likely Cause | Quick Fix Link |
|---------|--------------|----------------|
| "OAuth mode requires NEXTCLOUD_HOST" | Missing environment variable | [Missing NEXTCLOUD_HOST](#missing-nextcloud_host) |
| "OAuth mode requires client credentials OR dynamic registration" | OIDC apps not configured | [Missing OIDC Apps](#missing-or-misconfigured-oidc-apps) |
| "PKCE support validation failed" | OIDC app doesn't advertise PKCE | [PKCE Not Advertised](#pkce-not-advertised) |
| "Stored client has expired" | Dynamic client expired | [Client Expired](#client-expired) |
| Only seeing Notes tools (7 instead of 90+) | Limited OAuth scopes granted | [Limited Scopes](#limited-scopes---only-seeing-notes-tools) |
| HTTP 401 for Notes API | Bearer token patch missing | [Bearer Token Auth Fails](#bearer-token-authentication-fails) |
| "OIDC discovery failed" | Network or configuration issue | [Discovery Failed](#oidc-discovery-failed) |
| "Permission denied" on .nextcloud_oauth_client.json | File permissions issue | [File Permission Error](#file-permission-error) |

## Configuration Issues

### Missing NEXTCLOUD_HOST

**Error Message**:
```
OAuth mode requires NEXTCLOUD_HOST environment variable
```

**Cause**: The `NEXTCLOUD_HOST` environment variable is not set or empty.

**Solution**:

1. Add to your `.env` file:
   ```bash
   NEXTCLOUD_HOST=https://your.nextcloud.instance.com
   ```

2. Reload environment variables:
   ```bash
   export $(grep -v '^#' .env | xargs)
   ```

3. Verify it's set:
   ```bash
   echo $NEXTCLOUD_HOST
   # Should output: https://your.nextcloud.instance.com
   ```

---

### Missing or Misconfigured OIDC Apps

**Error Message**:
```
OAuth mode requires either client credentials OR dynamic client registration
```

**Cause**: The required Nextcloud OIDC apps are either:
- Not installed
- Not enabled
- Missing configuration

**Solution**:

**Step 1**: Verify both apps are installed:

```bash
# Check installed apps
php occ app:list | grep -E "oidc|user_oidc"

# Should show:
#  - oidc: enabled
#  - user_oidc: enabled
```

If not installed:
1. Open Nextcloud as administrator
2. Navigate to **Apps** → **Security**
3. Install **"OIDC"** (OIDC Identity Provider)
4. Install **"OpenID Connect user backend"** (user_oidc)
5. Enable both apps

**Step 2**: Enable dynamic client registration:

1. Go to **Settings** → **OIDC** (Administration)
2. Enable **"Allow dynamic client registration"**

**Step 3**: Configure Bearer token validation:

```bash
php occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean
```

**Step 4**: Verify discovery endpoint:

```bash
curl https://your.nextcloud.instance.com/.well-known/openid-configuration | jq '.registration_endpoint'

# Should output:
# "https://your.nextcloud.instance.com/apps/oidc/register"
```

**Alternative**: Use pre-configured client credentials:

```bash
# Register client via CLI
php occ oidc:create \
  --name="Nextcloud MCP Server" \
  --type=confidential \
  --redirect-uri="http://localhost:8000/oauth/callback"

# Add to .env
echo "NEXTCLOUD_OIDC_CLIENT_ID=<client-id>" >> .env
echo "NEXTCLOUD_OIDC_CLIENT_SECRET=<client-secret>" >> .env
```

---

### Client Expired

**Error Message**:
```
Stored client has expired
```

**Cause**: Dynamically registered OAuth clients expire (default: 1 hour).

**Solution**:

**Option 1: Restart the Server** (Automatic re-registration)

```bash
uv run nextcloud-mcp-server --oauth
# Server automatically re-registers if credentials expired
```

**Option 2: Use Pre-configured Credentials** (Recommended for production)

```bash
# Register permanent client via Nextcloud CLI
php occ oidc:create \
  --name="Nextcloud MCP Server" \
  --type=confidential \
  --redirect-uri="http://localhost:8000/oauth/callback"

# Add to .env
NEXTCLOUD_OIDC_CLIENT_ID=<from-output>
NEXTCLOUD_OIDC_CLIENT_SECRET=<from-output>
```

Pre-configured clients don't expire.

**Option 3: Increase Expiration Time**

```bash
# Via Nextcloud CLI (default: 3600 seconds = 1 hour)
php occ config:app:set oidc expire_time --value "86400"  # 24 hours
```

---

### File Permission Error

**Error Message**:
```
Permission denied when reading/writing .nextcloud_oauth_client.json
```

**Cause**: The server cannot access the OAuth client storage file.

**Solution**:

```bash
# Check file permissions
ls -la .nextcloud_oauth_client.json

# Fix file permissions (owner read/write only)
chmod 600 .nextcloud_oauth_client.json

# Ensure directory is writable
chmod 755 $(dirname .nextcloud_oauth_client.json)

# If file doesn't exist, ensure directory is writable
mkdir -p $(dirname .nextcloud_oauth_client.json)
```

For custom storage paths:
```bash
# Set custom path in .env
NEXTCLOUD_OIDC_CLIENT_STORAGE=/path/to/custom/oauth_client.json

# Ensure directory exists and is writable
mkdir -p $(dirname /path/to/custom/oauth_client.json)
chmod 755 $(dirname /path/to/custom/oauth_client.json)
```

---

## Discovery and Connection Issues

### OIDC Discovery Failed

**Error Message**:
```
OIDC discovery failed
Cannot reach OIDC discovery endpoint
```

**Cause**: The server cannot reach the Nextcloud OIDC discovery endpoint.

**Solution**:

**Step 1**: Verify Nextcloud URL is correct:

```bash
echo $NEXTCLOUD_HOST
# Should be full URL: https://your.nextcloud.instance.com
```

**Step 2**: Test discovery endpoint manually:

```bash
curl https://your.nextcloud.instance.com/.well-known/openid-configuration

# Should return JSON with OIDC configuration
# {
#   "issuer": "https://your.nextcloud.instance.com",
#   "authorization_endpoint": "https://your.nextcloud.instance.com/apps/oidc/authorize",
#   ...
# }
```

**Step 3**: Check network connectivity:

```bash
# Test basic connectivity
ping your.nextcloud.instance.com

# Test HTTPS
curl -I https://your.nextcloud.instance.com
```

**Step 4**: Verify both OIDC apps are enabled:

```bash
php occ app:list | grep -E "oidc|user_oidc"
```

**Step 5**: Check firewall rules (if using Docker):

```bash
# Check if MCP server can reach Nextcloud
docker exec nextcloud-mcp-server curl https://your.nextcloud.instance.com/.well-known/openid-configuration
```

---

## Authentication Issues

### Bearer Token Authentication Fails

**Error Message**:
```
HTTP 401 Unauthorized when calling Nextcloud APIs
```

**Symptoms**:
- OCS APIs work (`/ocs/v2.php/cloud/capabilities`)
- App APIs fail (`/apps/notes/api/`, `/apps/calendar/`, etc.)

**Cause**: The `user_oidc` app's CORS middleware interferes with Bearer token authentication for non-OCS endpoints.

**Solution**: Apply the Bearer token patch to `user_oidc` app.

See [Upstream Status](oauth-upstream-status.md#1-bearer-token-support-for-non-ocs-endpoints) for details.

**Quick Patch**:

```bash
# SSH into Nextcloud server
cd /path/to/nextcloud/apps/user_oidc

# Edit lib/User/Backend.php
# Add this line before each return statement in getCurrentUserId() method:
$this->session->set('app_api', true);

# Lines to modify: ~243, ~310, ~315, ~337
```

**Test the fix**:

```bash
# Get an OAuth token (from MCP client or test)
TOKEN="your_access_token"

# Test Notes API
curl -H "Authorization: Bearer $TOKEN" \
  https://your.nextcloud.instance.com/apps/notes/api/v1/notes

# Should return notes JSON (not 401)
```

---

### PKCE Not Advertised

**Error Message**:
```
ERROR: OIDC CONFIGURATION ERROR - Missing PKCE Support Advertisement
⚠️  MCP clients (like Claude Code) WILL REJECT this provider!
```

**Cause**: The OIDC discovery endpoint doesn't include `code_challenge_methods_supported` field.

**Impact**:
- Some MCP clients may refuse to connect
- Standards compliance issue (RFC 8414)
- **Functionality still works** (PKCE is accepted, just not advertised)

**Solution**:

**Short-term**: The MCP server logs a warning but continues. OAuth flow still works.

**Long-term**: Update the `oidc` app to advertise PKCE support.

See [Upstream Status](oauth-upstream-status.md#2-pkce-support-advertisement-in-discovery) for tracking.

**Verify**:

```bash
curl https://your.nextcloud.instance.com/.well-known/openid-configuration | jq '.code_challenge_methods_supported'

# Should return:
# ["S256", "plain"]

# If null, PKCE isn't advertised (but still works)
```

---

## Runtime Issues

### MCP Client Can't Authenticate

**Symptoms**:
- Client connects but OAuth flow fails
- Authorization redirects don't work
- Token exchange fails

**Diagnosis**:

**Step 1**: Verify OAuth is configured correctly:

```bash
uv run nextcloud-mcp-server --oauth --log-level debug
```

Look for:
```
INFO     OAuth initialization complete
INFO     MCP server ready at http://127.0.0.1:8000
```

**Step 2**: Check OIDC discovery:

```bash
curl https://your.nextcloud.instance.com/.well-known/openid-configuration
```

**Step 3**: Verify MCP server URL matches client expectations:

```bash
echo $NEXTCLOUD_MCP_SERVER_URL
# Should match the URL clients use to connect
# Default: http://localhost:8000
```

If MCP server is on a different host/port, update:
```bash
NEXTCLOUD_MCP_SERVER_URL=http://actual-host:actual-port
```

**Step 4**: Check redirect URI configuration:

For pre-configured clients, ensure redirect URI matches:
```bash
# Client redirect URI should be:
http://your-mcp-server-url/oauth/callback

# Example for local server:
http://localhost:8000/oauth/callback
```

---

### Tools Return 401 Errors

**Symptoms**:
- OAuth flow completes successfully
- Token is valid
- MCP tools return 401 errors

**Cause**: Bearer token not working with Nextcloud APIs.

**Solution**: See [Bearer Token Authentication Fails](#bearer-token-authentication-fails) above.

---

### Limited Scopes - Only Seeing Notes Tools

**Symptoms**:
- MCP client (e.g., Claude Code) successfully connects via OAuth
- Only Notes tools are available (7 tools instead of 90+)
- Token scopes show only `mcp:notes:read` and `mcp:notes:write`

**Cause**: During the OAuth consent flow, the user only granted access to Notes scopes, or the client only requested those scopes.

**Diagnosis**:

Check what scopes the client has been granted:

```bash
# View registered clients and their allowed scopes
php occ oidc:list | jq '.[] | select(.name | contains("Claude Code")) | {name, allowed_scopes}'
```

Look for the client's `allowed_scopes` field. If it's empty or only contains notes scopes, that's the issue.

**Solution**:

**Option 1: Delete Client and Reconnect** (Recommended for MCP clients)

```bash
# Find the client ID
php occ oidc:list | jq '.[] | select(.name | contains("Claude Code")) | {name, client_id}'

# Delete the client
php occ oidc:delete <client_id>

# Reconnect from Claude Code
# This will trigger a new OAuth flow where you can grant all scopes
```

When reconnecting, you'll see a consent screen listing all available scopes. Make sure to approve all the scopes you want the client to access.

**Option 2: Update Client Scopes via CLI**

```bash
# Update allowed scopes for an existing client
php occ oidc:update <client_id> \
  --allowed-scopes "openid profile email mcp:notes:read mcp:notes:write mcp:calendar:read mcp:calendar:write mcp:contacts:read mcp:contacts:write mcp:cookbook:read mcp:cookbook:write mcp:deck:read mcp:deck:write mcp:tables:read mcp:tables:write mcp:files:read mcp:files:write mcp:sharing:read mcp:sharing:write"

# User will need to reconnect to get new token with updated scopes
```

**Verify Available Scopes**:

Check what scopes the MCP server advertises:

```bash
curl http://localhost:8001/.well-known/oauth-protected-resource | jq '.scopes_supported'

# Should show all 16 scope categories:
# - openid
# - mcp:notes:read, mcp:notes:write
# - mcp:calendar:read, mcp:calendar:write
# - mcp:contacts:read, mcp:contacts:write
# - mcp:cookbook:read, mcp:cookbook:write
# - mcp:deck:read, mcp:deck:write
# - mcp:tables:read, mcp:tables:write
# - mcp:files:read, mcp:files:write
# - mcp:sharing:read, mcp:sharing:write
```

**Understanding Scope Filtering**:

The MCP server dynamically filters tools based on the scopes in your access token:
- Check server logs for: `✂️ JWT scope filtering: X/90 tools available for scopes: {...}`
- This shows how many tools are visible vs total available
- Each tool requires specific scopes (read and/or write)

**Available Scope Categories**:

| Scope Prefix | Nextcloud App | Read Operations | Write Operations |
|--------------|---------------|-----------------|------------------|
| `mcp:notes:*` | Notes | Get, search, list | Create, update, delete, append |
| `mcp:calendar:*` | Calendar (CalDAV) | Get events, todos, calendars | Create/update/delete events, todos |
| `mcp:contacts:*` | Contacts (CardDAV) | Get contacts, address books | Create/update/delete contacts |
| `mcp:cookbook:*` | Cookbook | Get recipes, search | Create/update recipes |
| `mcp:deck:*` | Deck | Get boards, cards | Create/update boards, cards |
| `mcp:tables:*` | Tables | Get rows, tables | Create/update/delete rows |
| `mcp:files:*` | Files (WebDAV) | List, read files | Upload, delete, move files |
| `mcp:sharing:*` | Sharing | Get shares | Create/update shares |

---

## Switching Authentication Modes

### From BasicAuth to OAuth

```bash
# 1. Remove or comment out USERNAME/PASSWORD in .env
sed -i 's/^NEXTCLOUD_USERNAME/#NEXTCLOUD_USERNAME/' .env
sed -i 's/^NEXTCLOUD_PASSWORD/#NEXTCLOUD_PASSWORD/' .env

# 2. Ensure NEXTCLOUD_HOST is set
grep NEXTCLOUD_HOST .env

# 3. Restart server with OAuth
export $(grep -v '^#' .env | xargs)
uv run nextcloud-mcp-server --oauth
```

### From OAuth to BasicAuth

```bash
# 1. Add USERNAME/PASSWORD to .env
echo "NEXTCLOUD_USERNAME=your-username" >> .env
echo "NEXTCLOUD_PASSWORD=your-password" >> .env

# 2. Restart server (BasicAuth auto-detected)
export $(grep -v '^#' .env | xargs)
uv run nextcloud-mcp-server --no-oauth
```

---

## Advanced Debugging

### Enable Debug Logging

```bash
uv run nextcloud-mcp-server --oauth --log-level debug
```

Look for:
- OIDC discovery details
- Client registration attempts
- Token validation logs
- API request/response details

### Test Discovery Endpoint

```bash
# Full discovery response
curl https://your.nextcloud.instance.com/.well-known/openid-configuration | jq

# Check specific fields
curl https://your.nextcloud.instance.com/.well-known/openid-configuration | jq '{
  issuer,
  authorization_endpoint,
  token_endpoint,
  userinfo_endpoint,
  registration_endpoint,
  code_challenge_methods_supported
}'
```

### Test Token Validation

```bash
# Get userinfo with token
curl -H "Authorization: Bearer $TOKEN" \
  https://your.nextcloud.instance.com/apps/oidc/userinfo

# Should return user info:
# {
#   "sub": "username",
#   "preferred_username": "username",
#   "name": "Display Name",
#   ...
# }
```

### Test Nextcloud API Access

```bash
# Test OCS API (should work)
curl -H "Authorization: Bearer $TOKEN" \
  "$NEXTCLOUD_HOST/ocs/v2.php/cloud/capabilities?format=json" \
  -H "OCS-APIRequest: true"

# Test app API (requires patch)
curl -H "Authorization: Bearer $TOKEN" \
  "$NEXTCLOUD_HOST/apps/notes/api/v1/notes"
```

---

## Getting Help

If you continue to experience issues:

### 1. Collect Diagnostic Information

```bash
# Server version
uv run nextcloud-mcp-server --version

# Python version
python3 --version

# Server logs with debug
uv run nextcloud-mcp-server --oauth --log-level debug 2>&1 | tee mcp-server.log

# OIDC discovery
curl https://your.nextcloud.instance.com/.well-known/openid-configuration > oidc-discovery.json

# Nextcloud version
# Check in Nextcloud admin panel or:
php occ -V
```

### 2. Check Documentation

- [OAuth Architecture](oauth-architecture.md) - How OAuth works
- [OAuth Setup Guide](oauth-setup.md) - Configuration steps
- [Upstream Status](oauth-upstream-status.md) - Required patches
- [Configuration](configuration.md) - Environment variables

### 3. Open an Issue

If problems persist, [open an issue](https://github.com/cbcoutinho/nextcloud-mcp-server/issues) with:

- **Error messages** (full text)
- **Server logs** (with `--log-level debug`)
- **OIDC discovery response** (from curl command above)
- **Nextcloud version**
- **OIDC app versions** (`oidc` and `user_oidc`)
- **Steps to reproduce**
- **Environment details** (OS, Python version, Docker vs local)

---

## See Also

- [OAuth Quick Start](quickstart-oauth.md) - Get started quickly
- [OAuth Setup Guide](oauth-setup.md) - Detailed configuration
- [OAuth Architecture](oauth-architecture.md) - Technical details
- [Upstream Status](oauth-upstream-status.md) - Required patches
- [General Troubleshooting](troubleshooting.md) - Non-OAuth issues
