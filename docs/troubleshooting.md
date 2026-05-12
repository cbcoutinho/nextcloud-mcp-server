# Troubleshooting

This guide covers common issues and solutions for the Nextcloud MCP server.

> **Multi-user / Login Flow v2 issues?** See the [Login Flow v2 troubleshooting section](login-flow-v2.md#troubleshooting) for app-password storage, provisioning loops, and OAuth issuer problems.

> **Upgrading from v0.57.x?** See the [Configuration Migration Guide](configuration-migration-v2.md) for help with new variable names.

## Configuration Issues (v0.58.0+)

### Issue: Deprecation warning for VECTOR_SYNC_ENABLED

**Symptom:**
```
WARNING: VECTOR_SYNC_ENABLED is deprecated. Please use ENABLE_SEMANTIC_SEARCH instead.
```

**Cause:** You're using the old variable name from v0.57.x.

**Solution:**
```bash
# In your .env file, replace:
VECTOR_SYNC_ENABLED=true

# With:
ENABLE_SEMANTIC_SEARCH=true
```

See [Configuration Migration Guide](configuration-migration-v2.md) for complete migration instructions.

---

### Issue: Deprecation warning for ENABLE_OFFLINE_ACCESS

**Symptom:**
```
WARNING: ENABLE_OFFLINE_ACCESS is deprecated. Please use ENABLE_BACKGROUND_OPERATIONS instead.
```

**Cause:** You're using the old variable name from v0.57.x.

**Solution:**

**If you have semantic search enabled:**
```bash
# In multi-user modes, you can remove ENABLE_OFFLINE_ACCESS entirely!
# ENABLE_SEMANTIC_SEARCH automatically enables background operations

# Before (v0.57.x):
ENABLE_OFFLINE_ACCESS=true
VECTOR_SYNC_ENABLED=true

# After (v0.58.0+):
ENABLE_SEMANTIC_SEARCH=true  # This is all you need!
```

**If you only want background operations (no semantic search):**
```bash
# Replace:
ENABLE_OFFLINE_ACCESS=true

# With:
ENABLE_BACKGROUND_OPERATIONS=true
```

---

### Issue: "Invalid MCP_DEPLOYMENT_MODE"

**Symptom:**
```
ValueError: Invalid MCP_DEPLOYMENT_MODE: 'oauth'. Valid values: single_user_basic, multi_user_basic, login_flow
```

**Cause:** Invalid value for `MCP_DEPLOYMENT_MODE`.

**Solution:**
Use one of the valid mode values:
```bash
MCP_DEPLOYMENT_MODE=single_user_basic   # Single-user with username/app password
MCP_DEPLOYMENT_MODE=multi_user_basic    # Multi-user BasicAuth pass-through
MCP_DEPLOYMENT_MODE=login_flow       # Multi-user via Login Flow v2 (recommended)
```

Or remove `MCP_DEPLOYMENT_MODE` to use automatic detection.

---

### Issue: Missing TOKEN_ENCRYPTION_KEY when semantic search enabled

**Symptom:**
```
Error: [login_flow] TOKEN_ENCRYPTION_KEY is required when ENABLE_SEMANTIC_SEARCH is enabled
```

**Cause:** In multi-user modes, semantic search automatically enables background operations, which require encrypted token storage.

**Solution:**
Generate an encryption key and add required token storage configuration:

```bash
# Generate encryption key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Add to .env:
TOKEN_ENCRYPTION_KEY=<generated-key>
TOKEN_STORAGE_DB=/app/data/tokens.db
```

**Why this happens:**
- v0.58.0+ automatically enables background operations when `ENABLE_SEMANTIC_SEARCH=true` in multi-user modes
- Background operations need encrypted refresh token storage
- This simplifies configuration but requires the encryption infrastructure

See [Configuration Guide - Semantic Search](configuration.md#semantic-search-configuration-optional) for details.

---

### Issue: Both old and new variable names set

**Symptom:**
```
WARNING: Both ENABLE_SEMANTIC_SEARCH and VECTOR_SYNC_ENABLED are set. Using ENABLE_SEMANTIC_SEARCH.
```

**Cause:** You have both the old and new variable names in your configuration.

**Solution:**
Remove the old variable name:
```bash
# Remove this line:
VECTOR_SYNC_ENABLED=true

# Keep this line:
ENABLE_SEMANTIC_SEARCH=true
```

The server will use the new name and ignore the old one, but it's cleaner to remove the old variable entirely.

---

## Multi-User / Login Flow v2 Issues

For multi-user deployment issues — provisioning loops, app-password storage, OAuth issuer endpoints, scope enforcement — see the [Login Flow v2 troubleshooting section](login-flow-v2.md#troubleshooting).

### Switching deployment modes

```bash
# To Single-User BasicAuth: set NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD
# To Multi-User BasicAuth pass-through: MCP_DEPLOYMENT_MODE=multi_user_basic (no creds)
# To Login Flow v2: MCP_DEPLOYMENT_MODE=login_flow (no creds; also the default fallback)
```

Restart the server after changing modes. The active mode is logged at startup; you can also set `MCP_DEPLOYMENT_MODE` explicitly to fail fast if the env vars don't match.

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
   curl http://localhost:8000/health/live

   # Test from network (if using --host 0.0.0.0)
   curl http://<server-ip>:8000/health/live
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

**Cause:** Various issues including memory limits or uncaught exceptions.

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

---

## Connection Issues

### Issue: MCP client can't authenticate

**Cause:** Auth flow failing or credentials invalid.

**Solution:**

**For BasicAuth modes:**
1. Verify credentials work:
   ```bash
   curl -u "$NEXTCLOUD_USERNAME:$NEXTCLOUD_PASSWORD" \
     "$NEXTCLOUD_HOST/ocs/v2.php/cloud/capabilities" \
     -H "OCS-APIRequest: true"
   ```

**For Login Flow v2 mode:**
1. Verify the server starts the OAuth issuer:
   ```bash
   uv run nextcloud-mcp-server --oauth --log-level debug
   # Look for "OAuth initialization complete"
   ```

2. Verify `NEXTCLOUD_MCP_SERVER_URL` matches the URL clients use to connect:
   ```bash
   echo $NEXTCLOUD_MCP_SERVER_URL
   ```

3. See [Login Flow v2 troubleshooting](login-flow-v2.md#troubleshooting) for app-password and provisioning issues.

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

3. **Test API directly with Basic Auth:**
   ```bash
   curl -u "$NEXTCLOUD_USERNAME:$NEXTCLOUD_PASSWORD" \
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

### 2. Test Nextcloud Connectivity

```bash
# Verify Nextcloud is reachable from the MCP server
curl -I "$NEXTCLOUD_HOST/status.php"

# With Basic Auth (Single-User or Multi-User BasicAuth modes)
curl -u "$NEXTCLOUD_USERNAME:$NEXTCLOUD_PASSWORD" \
  "$NEXTCLOUD_HOST/ocs/v2.php/cloud/capabilities?format=json" \
  -H "OCS-APIRequest: true"
```

For Login Flow v2 mode, see [Login Flow v2 troubleshooting](login-flow-v2.md#troubleshooting).

### 3. Check Versions

```bash
# MCP Server version
uv run nextcloud-mcp-server --version

# Python version
python3 --version

# Nextcloud version (check in admin panel)
```

### 4. Open an Issue

If problems persist, open an issue on the [GitHub repository](https://github.com/cbcoutinho/nextcloud-mcp-server/issues) with:

- **Server logs** (with `--log-level debug`)
- **Nextcloud version**
- **Deployment mode** (single_user_basic / multi_user_basic / login_flow)
- **Error messages**
- **Steps to reproduce**
- **Environment details** (OS, Python version, Docker vs local)

---

## See Also

- [Authentication](authentication.md) - Authentication modes
- [Login Flow v2](login-flow-v2.md) - Multi-user setup, scope reference, troubleshooting FAQ
- [Configuration](configuration.md) - Environment variables
- [Running the Server](running.md) - Server options
