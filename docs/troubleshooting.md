# Troubleshooting

This guide covers common issues and solutions for the Nextcloud MCP server.

> **OAuth-specific issues?** See the dedicated [OAuth Troubleshooting Guide](oauth-troubleshooting.md) for OAuth authentication problems, OIDC discovery issues, token validation failures, and more.

## OAuth Issues (Quick Reference)

### Issue: "OAuth mode requires NEXTCLOUD_HOST environment variable"

**Cause:** The `NEXTCLOUD_HOST` environment variable is not set or empty.

**Solution:**

```bash
# Ensure NEXTCLOUD_HOST is set in your .env file
echo "NEXTCLOUD_HOST=https://your.nextcloud.instance.com" >> .env

# Load environment variables
export $(grep -v '^#' .env | xargs)

# Verify it's set
echo $NEXTCLOUD_HOST
```

---

### Issue: "OAuth mode requires either client credentials OR dynamic client registration"

**Cause:** The required Nextcloud OIDC apps are either:
1. Not installed (both `oidc` and `user_oidc` apps are required)
2. Don't have dynamic client registration enabled
3. Aren't providing a registration endpoint

**Solution:**

**Option 1: Enable dynamic client registration**

1. Verify **both** OIDC apps are installed:
   - Navigate to Nextcloud **Apps** → **Security**
   - Install **"OIDC"** (OIDC Identity Provider app) if not present
   - Install **"OpenID Connect user backend"** (user_oidc app) if not present

2. Enable dynamic client registration:
   - Go to **Settings** → **OIDC** (Administration)
   - Enable "Allow dynamic client registration"

3. Configure Bearer token validation:
   ```bash
   # Required for user_oidc app to validate tokens
   php occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean
   ```

3. Verify the registration endpoint exists:
   ```bash
   curl https://your.nextcloud.instance.com/.well-known/openid-configuration | jq '.registration_endpoint'
   # Should output: "https://your.nextcloud.instance.com/apps/oidc/register"
   ```

**Option 2: Provide pre-configured credentials**

Register a client and add credentials to `.env`:

```bash
# On your Nextcloud server
php occ oidc:create \
  --name="Nextcloud MCP Server" \
  --type=confidential \
  --redirect-uri="http://localhost:8000/oauth/callback"

# Add to .env
echo "NEXTCLOUD_OIDC_CLIENT_ID=<from-output>" >> .env
echo "NEXTCLOUD_OIDC_CLIENT_SECRET=<from-output>" >> .env
```

See [OAuth Setup Guide](oauth-setup.md) for detailed instructions.

---

### Issue: "Stored client has expired"

**Cause:** Dynamically registered OAuth clients expire (default: 1 hour).

**Solution:**

**Option 1: Restart the server** (automatic re-registration)

```bash
# Server checks credentials at startup and re-registers if expired
uv run nextcloud-mcp-server --oauth
```

**Option 2: Use pre-configured credentials** (recommended for production)

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

**Option 3: Increase expiration time**

```bash
# Via Nextcloud occ command (default: 3600 seconds)
php occ config:app:set oidc expire_time --value "86400"  # 24 hours
```

---

### Issue: "HTTP 401 Unauthorized" when calling Nextcloud APIs

**Cause:** OAuth Bearer tokens may not work with certain Nextcloud endpoints due to session handling in the CORS middleware.

**Background:** The `user_oidc` app's CORS middleware interferes with Bearer token authentication for non-OCS endpoints (like Notes API). This affects app-specific APIs but not OCS APIs.

**Solution:**

A patch for the `user_oidc` app is required to fix Bearer token support. See [oauth2-bearer-token-session-issue.md](oauth2-bearer-token-session-issue.md) for:
- Detailed explanation of the issue
- Patch to apply to the `user_oidc` app
- Link to upstream pull request

**Affected endpoints:**
- Notes API (`/apps/notes/api/`)
- Other app-specific endpoints

**Unaffected endpoints:**
- OCS APIs (`/ocs/v2.php/`)
- Capabilities endpoint

---

### Issue: "Permission denied" when reading/writing OAuth client credentials file

**Cause:** The server cannot access the OAuth client storage file (default: `.nextcloud_oauth_client.json`).

**Solution:**

```bash
# Check file permissions
ls -la .nextcloud_oauth_client.json

# Fix file permissions (should be 0600 - owner read/write only)
chmod 600 .nextcloud_oauth_client.json

# Ensure the directory is writable
chmod 755 $(dirname .nextcloud_oauth_client.json)

# If the file doesn't exist, ensure the directory is writable so it can be created
mkdir -p $(dirname .nextcloud_oauth_client.json)
```

---

### Issue: "OIDC discovery failed" or "Cannot reach OIDC discovery endpoint"

**Cause:** The server cannot reach the Nextcloud OIDC discovery endpoint.

**Solution:**

1. Verify the Nextcloud URL is correct:
   ```bash
   echo $NEXTCLOUD_HOST
   # Should be the full URL: https://your.nextcloud.instance.com
   ```

2. Test the discovery endpoint manually:
   ```bash
   curl https://your.nextcloud.instance.com/.well-known/openid-configuration
   # Should return JSON with OIDC configuration
   ```

3. Check network connectivity:
   ```bash
   ping your.nextcloud.instance.com
   ```

4. Verify **both** OIDC apps are installed and enabled in Nextcloud:
   - `oidc` - OIDC Identity Provider
   - `user_oidc` - OpenID Connect user backend

5. Check firewall rules if using Docker

---

### Switching Between OAuth and BasicAuth

#### To switch from BasicAuth to OAuth:

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

#### To switch from OAuth to BasicAuth:

```bash
# 1. Add USERNAME/PASSWORD to .env
echo "NEXTCLOUD_USERNAME=your-username" >> .env
echo "NEXTCLOUD_PASSWORD=your-password" >> .env

# 2. Restart server (BasicAuth auto-detected, or use --no-oauth)
export $(grep -v '^#' .env | xargs)
uv run nextcloud-mcp-server --no-oauth
```

---

### For More OAuth Help

See the dedicated **[OAuth Troubleshooting Guide](oauth-troubleshooting.md)** for:
- Bearer token authentication failures
- PKCE validation errors
- Token validation issues
- Client registration problems
- Advanced OAuth debugging
- And much more...

---

## Configuration Issues

### Issue: Environment variables not loaded

**Cause:** Environment variables from `.env` file are not loaded into the shell.

**Solution:**

**On Linux/macOS:**
```bash
# Load all variables from .env
export $(grep -v '^#' .env | xargs)

# Verify variables are set
env | grep NEXTCLOUD
```

**On Windows (PowerShell):**
```powershell
# Load variables from .env
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)\s*=\s*(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
    }
}

# Verify variables are set
Get-ChildItem Env:NEXTCLOUD*
```

**With Docker:**
```bash
# Docker automatically loads .env when using --env-file
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

---

### Issue: ".env file not found"

**Cause:** The `.env` file doesn't exist or is in the wrong location.

**Solution:**

```bash
# Create .env from sample
cp env.sample .env

# Edit with your Nextcloud details
nano .env  # or vim, code, etc.

# Ensure you're in the correct directory when running commands
pwd  # Should be in the project directory containing .env
```

---

### Issue: "Invalid Nextcloud credentials"

**Cause:** BasicAuth credentials are incorrect or the app password has been revoked.

**Solution:**

1. **Verify username:**
   ```bash
   # Username should match your Nextcloud login
   echo $NEXTCLOUD_USERNAME
   ```

2. **Generate a new app password:**
   - Log in to Nextcloud
   - Go to **Settings** → **Security**
   - Under "Devices & sessions", create a new app password
   - Update `.env` with the new password

3. **Test credentials manually:**
   ```bash
   curl -u "$NEXTCLOUD_USERNAME:$NEXTCLOUD_PASSWORD" \
     "$NEXTCLOUD_HOST/ocs/v2.php/cloud/capabilities" \
     -H "OCS-APIRequest: true"
   # Should return XML with capabilities
   ```

---

## Server Issues

### Issue: "Address already in use" / Port conflict

**Cause:** Another process is using port 8000.

**Solution:**

**Option 1: Use a different port**
```bash
uv run nextcloud-mcp-server --port 8080
```

**Option 2: Find and kill the process using the port**
```bash
# On Linux/macOS
lsof -ti:8000 | xargs kill -9

# On Windows
netstat -ano | findstr :8000
taskkill /PID <pid> /F
```

**Option 3: Stop other MCP server instances**
```bash
# Check for running instances
ps aux | grep nextcloud-mcp-server

# Kill specific process
kill <pid>
```

---

### Issue: Server starts but can't connect

**Cause:** Server is bound to localhost only, or firewall is blocking connections.

**Solution:**

1. **Check server binding:**
   ```bash
   # Bind to all interfaces to allow network access
   uv run nextcloud-mcp-server --host 0.0.0.0 --port 8000
   ```

2. **Test connectivity:**
   ```bash
   # Test from same machine
   curl http://localhost:8000/health

   # Test from network (if using --host 0.0.0.0)
   curl http://<server-ip>:8000/health
   ```

3. **Check firewall:**
   ```bash
   # Linux (ufw)
   sudo ufw allow 8000/tcp

   # Linux (firewalld)
   sudo firewall-cmd --add-port=8000/tcp --permanent
   sudo firewall-cmd --reload
   ```

---

### Issue: Server crashes or restarts frequently

**Cause:** Various issues including memory limits, uncaught exceptions, or OAuth token expiration.

**Solution:**

1. **Check logs with debug level:**
   ```bash
   uv run nextcloud-mcp-server --log-level debug
   ```

2. **Monitor resource usage:**
   ```bash
   # Check memory and CPU
   top -p $(pgrep -f nextcloud-mcp-server)
   ```

3. **Use process manager for automatic restart:**
   ```bash
   # With systemd (see Running guide for full config)
   sudo systemctl restart nextcloud-mcp

   # With Docker Compose (includes restart: unless-stopped)
   docker-compose up -d
   ```

4. **Check for OAuth credential expiration** (if using dynamic registration):
   - See ["Stored client has expired"](#issue-stored-client-has-expired) above

---

## Connection Issues

### Issue: MCP client can't authenticate

**Cause:** OAuth flow failing or credentials invalid.

**Solution:**

**For OAuth:**
1. Verify OAuth is configured correctly:
   ```bash
   uv run nextcloud-mcp-server --oauth --log-level debug
   # Look for "OAuth initialization complete"
   ```

2. Check that OIDC app is accessible:
   ```bash
   curl https://your.nextcloud.instance.com/.well-known/openid-configuration
   ```

3. Verify MCP_SERVER_URL matches your setup:
   ```bash
   echo $NEXTCLOUD_MCP_SERVER_URL
   # Should match the URL clients use to connect
   ```

**For BasicAuth:**
1. Verify credentials work:
   ```bash
   curl -u "$NEXTCLOUD_USERNAME:$NEXTCLOUD_PASSWORD" \
     "$NEXTCLOUD_HOST/ocs/v2.php/cloud/capabilities" \
     -H "OCS-APIRequest: true"
   ```

---

### Issue: Tools return errors or don't work

**Cause:** Missing Nextcloud apps, incorrect permissions, or API issues.

**Solution:**

1. **Verify required Nextcloud apps are installed:**
   - Notes: Install "Notes" app
   - Calendar: Ensure CalDAV is enabled
   - Contacts: Ensure CardDAV is enabled
   - Deck: Install "Deck" app

2. **Check user permissions:**
   - Ensure the authenticated user has access to the resources
   - Check sharing permissions for shared resources

3. **Test API directly:**
   ```bash
   # Test Notes API
   curl -u "$NEXTCLOUD_USERNAME:$NEXTCLOUD_PASSWORD" \
     "$NEXTCLOUD_HOST/apps/notes/api/v1/notes"

   # Test with OAuth Bearer token
   curl -H "Authorization: Bearer $TOKEN" \
     "$NEXTCLOUD_HOST/apps/notes/api/v1/notes"
   ```

4. **Check server logs for specific errors:**
   ```bash
   uv run nextcloud-mcp-server --log-level debug
   ```

---

## Getting Help

If you continue to experience issues:

### 1. Enable Debug Logging

```bash
uv run nextcloud-mcp-server --log-level debug
```

Review the logs for specific error messages.

### 2. Verify OIDC Configuration (OAuth mode)

```bash
# Check OIDC discovery
curl https://your.nextcloud.instance.com/.well-known/openid-configuration

# Check registration endpoint exists
curl https://your.nextcloud.instance.com/.well-known/openid-configuration | jq '.registration_endpoint'
```

### 3. Test Nextcloud API Access

```bash
# Test OCS API (should work with OAuth)
curl -H "Authorization: Bearer $TOKEN" \
  "$NEXTCLOUD_HOST/ocs/v2.php/cloud/capabilities?format=json" \
  -H "OCS-APIRequest: true"

# Test app API (may need patch - see oauth2-bearer-token-session-issue.md)
curl -H "Authorization: Bearer $TOKEN" \
  "$NEXTCLOUD_HOST/apps/notes/api/v1/notes"
```

### 4. Check Versions

```bash
# MCP Server version
uv run nextcloud-mcp-server --version

# Python version
python3 --version

# Nextcloud version (check in admin panel)
```

### 5. Open an Issue

If problems persist, open an issue on the [GitHub repository](https://github.com/cbcoutinho/nextcloud-mcp-server/issues) with:

- **Server logs** (with `--log-level debug`)
- **Nextcloud version**
- **OIDC app version** (if using OAuth)
- **Error messages**
- **Steps to reproduce**
- **Environment details** (OS, Python version, Docker vs local)

---

## See Also

- **[OAuth Troubleshooting](oauth-troubleshooting.md)** - Dedicated OAuth troubleshooting guide
- [OAuth Setup Guide](oauth-setup.md) - OAuth configuration
- [OAuth Architecture](oauth-architecture.md) - How OAuth works
- [Upstream Status](oauth-upstream-status.md) - Required patches and upstream PRs
- [Configuration](configuration.md) - Environment variables
- [Running the Server](running.md) - Server options
