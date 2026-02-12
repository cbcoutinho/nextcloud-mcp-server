# Webhook Management Guide

This guide explains how to enable and disable webhooks for vector sync in each MCP server deployment mode. Webhooks enable near-real-time synchronization of content changes to the vector database, complementing the default polling-based sync.

**Related ADRs:**
- ADR-010: Webhook-Based Vector Sync
- ADR-020: Deployment Modes and Configuration Validation

## Prerequisites

Before enabling webhooks, ensure:

1. **Nextcloud 30+** with `webhook_listeners` app enabled
2. **[Astrolabe app](https://github.com/cbcoutinho/astrolabe)** installed in Nextcloud (provides settings UI and credentials API)
3. **MCP server** accessible from Nextcloud via HTTP(S)
4. **Vector sync enabled** on the MCP server

## Webhook Architecture Overview

The webhook system has two components:

1. **Webhook Registration** - Configuring Nextcloud to send change notifications to the MCP server
2. **Background Sync Credentials** - Allowing the MCP server to access Nextcloud APIs on behalf of users

Both must be configured for webhooks to function properly.

## Deployment Mode Specifics

### 1. Single-User BasicAuth

**Configuration:**
```bash
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password
VECTOR_SYNC_ENABLED=true
```

**Enable Webhooks:**
1. Register webhooks using occ commands (requires Nextcloud admin):
   ```bash
   # Enable webhook_listeners app
   php occ app:enable webhook_listeners

   # Register webhooks for vector sync
   php occ webhook_listeners:add \
     --event "OCP\Files\Events\Node\NodeCreatedEvent" \
     --uri "http://mcp-server:8000/webhooks/nextcloud" \
     --method POST

   # Repeat for other events (see Event Types below)
   ```

2. Optionally reduce polling frequency:
   ```bash
   VECTOR_SYNC_SCAN_INTERVAL=86400  # 24 hours
   ```

**Disable Webhooks:**
```bash
# List registered webhooks
php occ webhook_listeners:list

# Remove specific webhook by ID
php occ webhook_listeners:remove <webhook-id>
```

**Notes:**
- Simplest mode - admin credentials used for all operations
- No per-user provisioning required
- Background sync runs as the configured admin user

---

### 2. Multi-User BasicAuth Pass-Through

**Configuration:**
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
ENABLE_MULTI_USER_BASIC_AUTH=true
ENABLE_BACKGROUND_OPERATIONS=true
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/app/data/tokens.db
VECTOR_SYNC_ENABLED=true
# OAuth client for Astrolabe API access
NEXTCLOUD_OIDC_CLIENT_ID=<client-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<client-secret>
```

**Credential Architecture:**
This mode uses **two separate credential mechanisms**:

1. **OAuth Session** (for management API access, including webhooks):
   - Obtained via browser OAuth flow (`/oauth/login`)
   - Stores refresh token in MCP server's `tokens.db`
   - Used for webhook registration/management APIs

2. **App Password** (for background sync):
   - Generated in Nextcloud Security settings
   - Stored encrypted in Nextcloud's `oc_preferences` via Astrolabe
   - Used by background scanners to access Nextcloud APIs

**Enable Webhooks:**

#### Step 1: Complete OAuth Login (for Management API)
Users must authorize the MCP server to access their Nextcloud:

1. Navigate to **Nextcloud Settings → Astrolabe** (Personal settings)
2. Click **"Authorize via OAuth"** under "Option 1"
3. Complete OAuth consent flow
4. Verify the page shows "Background Sync Access: Active"

#### Step 2: Configure App Password (for Background Sync)
Since OAuth refresh tokens have short expiry, users should also configure an app password:

1. Navigate to **Nextcloud Settings → Security**
2. Generate a new app password (name it "Astrolabe" or "MCP Server")
3. Return to **Nextcloud Settings → Astrolabe**
4. Under "Option 2: App Password", paste the app password
5. Click **Save**

#### Step 3: Register Webhooks (Admin)
Same as Single-User BasicAuth:
```bash
php occ webhook_listeners:add \
  --event "OCP\Files\Events\Node\NodeCreatedEvent" \
  --uri "http://mcp-server:8003/webhooks/nextcloud" \
  --method POST
```

**Disable Webhooks:**

*Per-User:*
1. Navigate to **Nextcloud Settings → Astrolabe**
2. Click **"Revoke Access"** (for OAuth tokens) or **"Revoke Access"** (for app password)

*System-Wide:*
```bash
php occ webhook_listeners:remove <webhook-id>
```

**Troubleshooting:**

If OAuth login fails with "Access forbidden - Your client is not authorized":
1. Check if OAuth client is registered:
   ```sql
   SELECT id, name, client_identifier FROM oc_oidc_clients
   WHERE dcr = 1 ORDER BY id DESC LIMIT 5;
   ```
2. Restart MCP server to trigger DCR re-registration
3. Verify `NEXTCLOUD_OIDC_CLIENT_ID` and `NEXTCLOUD_OIDC_CLIENT_SECRET` are set

If background sync fails with "User no longer provisioned":
1. Verify app password is stored:
   ```sql
   SELECT userid, configkey FROM oc_preferences
   WHERE appid = 'astrolabe' AND userid = 'username';
   ```
2. Ensure user completed **both** OAuth login AND app password setup

---

### 3. OAuth Single-Audience (Default OAuth Mode)

**Configuration:**
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
# No NEXTCLOUD_USERNAME/PASSWORD
ENABLE_BACKGROUND_OPERATIONS=true
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/app/data/tokens.db
VECTOR_SYNC_ENABLED=true
```

**Enable Webhooks:**

#### Step 1: User Provisioning
Users authorize via OAuth with `offline_access` scope:

1. MCP client initiates OAuth flow
2. User consents to requested scopes including `offline_access`
3. MCP server stores refresh token for background operations

Alternatively, via Astrolabe UI:
1. Navigate to **Nextcloud Settings → Astrolabe**
2. Click **"Authorize via OAuth"**
3. Complete consent flow

#### Step 2: Register Webhooks (Admin)
```bash
php occ webhook_listeners:add \
  --event "OCP\Files\Events\Node\NodeCreatedEvent" \
  --uri "http://mcp-server:8001/webhooks/nextcloud" \
  --method POST
```

**Disable Webhooks:**

*Per-User:*
- Via Astrolabe UI: Click "Disable Indexing" or "Disconnect"
- Via MCP tool: Use `revoke_nextcloud_access` if available

*System-Wide:*
```bash
php occ webhook_listeners:remove <webhook-id>
```

---

### 4. OAuth Token Exchange (RFC 8693)

**Configuration:**
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
ENABLE_TOKEN_EXCHANGE=true
ENABLE_BACKGROUND_OPERATIONS=true
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/app/data/tokens.db
VECTOR_SYNC_ENABLED=true
```

**Enable/Disable Webhooks:**
Same process as OAuth Single-Audience. The token exchange happens transparently when the MCP server accesses Nextcloud APIs.

---

### 5. Smithery Stateless

**Configuration:**
- Configuration from session URL params
- `VECTOR_SYNC_ENABLED=false` (required)

**Webhooks:**
**Not supported.** This mode is stateless with no persistent storage or background operations.

---

## Webhook Event Types

Register these webhook events for full vector sync coverage:

### File/Note Events
```bash
# Use BeforeNodeDeletedEvent for deletions (includes node.id)
php occ webhook_listeners:add --event "OCP\Files\Events\Node\NodeCreatedEvent" --uri "$MCP_URL/webhooks/nextcloud"
php occ webhook_listeners:add --event "OCP\Files\Events\Node\NodeWrittenEvent" --uri "$MCP_URL/webhooks/nextcloud"
php occ webhook_listeners:add --event "OCP\Files\Events\Node\BeforeNodeDeletedEvent" --uri "$MCP_URL/webhooks/nextcloud"
```

### Calendar Events
```bash
php occ webhook_listeners:add --event "OCP\Calendar\Events\CalendarObjectCreatedEvent" --uri "$MCP_URL/webhooks/nextcloud"
php occ webhook_listeners:add --event "OCP\Calendar\Events\CalendarObjectUpdatedEvent" --uri "$MCP_URL/webhooks/nextcloud"
php occ webhook_listeners:add --event "OCP\Calendar\Events\CalendarObjectDeletedEvent" --uri "$MCP_URL/webhooks/nextcloud"
```

### Tables Events
```bash
php occ webhook_listeners:add --event "OCA\Tables\Event\RowAddedEvent" --uri "$MCP_URL/webhooks/nextcloud"
php occ webhook_listeners:add --event "OCA\Tables\Event\RowUpdatedEvent" --uri "$MCP_URL/webhooks/nextcloud"
php occ webhook_listeners:add --event "OCA\Tables\Event\RowDeletedEvent" --uri "$MCP_URL/webhooks/nextcloud"
```

## Security Considerations

### Webhook Authentication
Configure `WEBHOOK_SECRET` to require authentication for incoming webhooks:

```bash
# MCP Server
WEBHOOK_SECRET=<generate-random-secret>

# Nextcloud webhook registration
php occ webhook_listeners:add \
  --event "..." \
  --uri "$MCP_URL/webhooks/nextcloud" \
  --header "Authorization: Bearer <secret>"
```

### Token Storage
- Refresh tokens and app passwords are encrypted using `TOKEN_ENCRYPTION_KEY`
- Store the key securely (environment variable, secrets manager)
- Different users have isolated credential storage

## Monitoring

### MCP Server Logs
```bash
# Docker
docker compose logs mcp-multi-user-basic | grep -i webhook

# Key log messages
# - "Queued document from webhook: ..." - Success
# - "Webhook authentication failed" - Auth error
# - "User X no longer provisioned" - Missing credentials
```

### Nextcloud Logs
```bash
docker compose exec app cat /var/www/html/data/nextcloud.log | \
  jq 'select(.message | contains("webhook"))' | tail
```

### Database Checks
```sql
-- Check registered webhooks
SELECT * FROM oc_webhook_listeners;

-- Check OAuth clients
SELECT id, name, token_type FROM oc_oidc_clients WHERE dcr = 1;

-- Check user credentials stored by Astrolabe app
SELECT userid, configkey FROM oc_preferences WHERE appid = 'astrolabe';
```

## Common Issues

### "Access forbidden - Your client is not authorized to connect"
**Cause:** OAuth client registration expired or not present in Nextcloud
**Fix:** Restart MCP server to trigger DCR re-registration

### "User X no longer provisioned, stopping scanner"
**Cause:** Background sync credentials missing or expired
**Fix:** User must complete credential provisioning (see mode-specific steps)

### "Failed to fetch" in browser console during OAuth
**Cause:** Network issue between browser and MCP server callback endpoint
**Fix:** Verify MCP server is accessible at the configured `NEXTCLOUD_MCP_SERVER_URL`

### Webhooks not firing
**Causes:**
1. `webhook_listeners` app not enabled
2. Webhook not registered for the event type
3. Background job workers not running
**Fix:**
```bash
php occ app:enable webhook_listeners
php occ background:cron  # or configure systemd cron
```
