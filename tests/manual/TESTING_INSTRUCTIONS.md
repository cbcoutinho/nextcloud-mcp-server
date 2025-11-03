# ADR-004 OAuth Flow Testing Instructions

## Automated Integration Test (Recommended)

The ADR-004 Hybrid Flow is now fully tested via automated integration tests using Playwright:

```bash
# Run all ADR-004 tests
uv run pytest tests/server/oauth/test_adr004_hybrid_flow.py --browser firefox -v

# Run specific test
uv run pytest tests/server/oauth/test_adr004_hybrid_flow.py::test_adr004_hybrid_flow_tool_execution --browser firefox -v
```

These tests verify:
- ✅ PKCE code challenge/verifier flow
- ✅ MCP server intercepts OAuth callback
- ✅ Master refresh token storage
- ✅ Client receives MCP access token
- ✅ MCP session establishment with hybrid flow token
- ✅ Tool execution using stored refresh tokens
- ✅ Multiple operations without re-authentication

## Manual Test (Legacy)

For manual testing or debugging, you can use the standalone test script:

```bash
# Make sure port 8765 is available
lsof -ti:8765 | xargs kill -9 2>/dev/null

# Run the test
uv run python tests/manual/test_adr004_manual.py --provider nextcloud
```

## Expected Flow

### 1. Test Script Starts
```
======================================================================
ADR-004 MANUAL OAUTH FLOW TEST
======================================================================
Provider:          nextcloud
MCP Server:        http://localhost:8001
Nextcloud:         http://localhost:8080
======================================================================

✓ Generated PKCE challenge: gxQLsYDJ...
✓ Started callback server at http://localhost:8765/callback
```

### 2. Open OAuth URL in Browser
The script will print:
```
======================================================================
STEP 1: AUTHORIZE THE MCP SERVER
======================================================================

📋 Open this URL in your browser:

    http://localhost:8001/oauth/authorize?response_type=code&...

📌 What will happen:
   1. You'll be redirected to Nextcloud/Keycloak login
   2. Login with username: admin, password: admin
   3. You'll see a consent screen asking to authorize the MCP server
   4. Click 'Authorize' or 'Allow'
   5. You'll be redirected to localhost:8765/callback
   6. The authorization code will appear in the terminal
```

### 3. Browser Flow
1. **Nextcloud Login** - You see the Nextcloud login page
2. **Enter Credentials** - admin/admin
3. **Consent Screen** - "Authorize Nextcloud MCP Server (jwt) to access your account?"
4. **Click Authorize**
5. **Redirect Chain**:
   - Nextcloud redirects to: `http://localhost:8001/oauth/callback?code=...`
   - MCP server processes the code
   - MCP server redirects to: `http://localhost:8765/callback?code=mcp-code-...&state=...`
   - Browser reaches the test script's callback server
   - You see: "✓ Authorization Successful - You can close this window"

### 4. Test Script Continues
```
✓ Received authorization code!
Code: mcp-code-xyz...
✓ State parameter verified (CSRF protection)

======================================================================
STEP 2: EXCHANGE CODE FOR ACCESS TOKEN
======================================================================

✓ Successfully received access token
  Token: eyJhbGciOiJSUzI1Ni...
  Type: Bearer
  Expires: 3600s

======================================================================
STEP 3: CALL MCP TOOL WITH ACCESS TOKEN
======================================================================

✓ MCP tool call succeeded!
  Result: {...}

======================================================================
🎉 ADR-004 OAUTH FLOW TEST - SUCCESS
======================================================================
```

## Troubleshooting

### Browser Gets Stuck at "localhost:8765 refused to connect"

**Problem**: The callback server on port 8765 isn't accessible.

**Solutions**:
1. Check firewall isn't blocking port 8765
2. Verify the test script is still running
3. Check another process isn't using port 8765:
   ```bash
   lsof -ti:8765
   ```

### Browser Shows "localhost:8765 - ERR_CONNECTION_REFUSED"

**Problem**: The callback server stopped or never started.

**Solution**:
1. Check the test script output - it should say "✓ Started callback server"
2. Restart the test script
3. Manually test the callback server:
   ```bash
   curl http://localhost:8765/callback?code=test&state=test
   ```
   Should return HTML page with "Authorization Successful"

### "Session not found or expired" Error

**Problem**: Took too long between steps (>10 minutes).

**Solution**: Restart the test - sessions expire after 10 minutes.

### Client ID is None

**Problem**: OAuth client credentials not loaded.

**Solution**: Rebuild the MCP server:
```bash
docker-compose up --build -d mcp-oauth
```

### Nextcloud Shows "Invalid redirect_uri"

**Problem**: The redirect URI isn't registered for the OAuth client.

**Solution**: Check registered URIs:
```bash
docker compose exec db mariadb -u root -ppassword nextcloud -e \
  "SELECT c.client_identifier, r.redirect_uri FROM oc_oidc_clients c \
   LEFT JOIN oc_oidc_redirect_uris r ON c.id = r.client_id \
   WHERE c.name LIKE '%MCP%';"
```

Should show: `http://localhost:8001/oauth/callback`

## Manual Test Without Script

If the automated test doesn't work, you can test manually:

1. **Start callback server manually**:
   ```bash
   python3 -m http.server 8765
   ```

2. **Open OAuth URL in browser** (get from test script output or build manually):
   ```
   http://localhost:8001/oauth/authorize?response_type=code&client_id=test-mcp-client&redirect_uri=http://localhost:8765/callback&scope=openid+profile+email+offline_access&state=TEST&code_challenge=CHALLENGE&code_challenge_method=S256
   ```

3. **Complete login** at Nextcloud

4. **Browser should redirect** to `http://localhost:8765/callback?code=mcp-code-...&state=TEST`

5. **Copy the code** from the URL and exchange it:
   ```bash
   curl -X POST http://localhost:8001/oauth/token \
     -d "grant_type=authorization_code" \
     -d "code=<MCP_CODE_HERE>" \
     -d "code_verifier=<VERIFIER_HERE>" \
     -d "redirect_uri=http://localhost:8765/callback" \
     -d "client_id=test-mcp-client"
   ```

## Expected Database State After Success

```bash
# Check refresh token was stored
docker compose exec mcp-oauth sh -c \
  "sqlite3 /app/data/tokens.db 'SELECT user_id, created_at FROM refresh_tokens;'"
```

Should show an entry for the authenticated user.
