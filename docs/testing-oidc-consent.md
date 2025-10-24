# Testing OIDC Consent Feature

This guide explains how to test the OIDC consent feature using the development version of the OIDC app mounted into the Docker environment.

## Setup

### Volume Mount Configuration

The development OIDC app is mounted from `~/Software/oidc` into the container at `/opt/apps/oidc`:

```yaml
# docker-compose.yml
volumes:
  - ../Software/oidc:/opt/apps/oidc:ro
```

**Why mount outside `/var/www/html/`?**
- The Nextcloud container uses `rsync` to initialize `/var/www/html/` from the image
- Mounting inside that path causes conflicts (rsync tries to delete mounted directories)
- Mounting to `/opt/apps/oidc` avoids rsync entirely
- Nextcloud supports multiple app directories via the `apps_paths` configuration

**How multiple app paths work:**
- Nextcloud can load apps from multiple directories
- The post-installation hook registers `/opt/apps` as an additional app directory (index 2)
- Apps in default paths (index 0 and 1) are still available
- All directories are scanned for apps, but `/opt/apps` is read-only

This setup allows you to:
- Test changes without rebuilding containers
- Avoid needing npm/node in the container (JS already built on host)
- Iterate quickly on development
- Install other Nextcloud apps normally (custom_apps remains writable)

### How It Works

1. **Mount Development App**: Docker mounts `~/Software/oidc` to `/opt/apps/oidc` (outside Nextcloud's path)
2. **Register App Path**: The `10-install-oidc-app.sh` hook configures `/opt/apps` as an additional app directory
3. **Enable App**: The hook enables the OIDC app from `/opt/apps/oidc`
4. **Run Migrations**: Nextcloud detects pending migrations and runs them automatically
5. **Configure OIDC**: Dynamic client registration and PKCE are enabled

## Starting the Stack

```bash
cd ~/Projects/nextcloud-mcp-server

# Start fresh (recommended for first test)
docker compose down -v
docker compose up -d

# Wait for initialization (check logs)
docker compose logs -f app
```

The post-installation hooks will:
1. Configure custom_apps path (already done)
2. Enable OIDC app from mounted directory
3. Run database migrations (including consent table creation)
4. Configure OIDC settings

## Verifying Installation

### Before Container Restart

Before running `docker compose up -d`, the consent feature will NOT be active:
- ❌ No `oc_oidc_user_consents` table in database
- ❌ Migration 0015 not applied yet
- ❌ ConsentController class not loaded
- ❌ Consent routes not registered

You can verify this with:
```bash
# Check migrations applied (should stop at 0014)
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "SELECT version FROM oc_migrations WHERE app = 'oidc' ORDER BY version DESC LIMIT 3;" nextcloud

# Check for consent table (should return empty)
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "SHOW TABLES LIKE 'oc_oidc_user_consents';" nextcloud
```

### After Container Restart

After `docker compose up -d` with the mounted OIDC directory, the consent feature should be active:
- ✅ `oc_oidc_user_consents` table exists
- ✅ Migration 0015 (Version0015Date20251123100100) applied
- ✅ ConsentController routes registered
- ✅ Consent screen appears during OAuth flows

### Check App Status

```bash
docker compose exec app php occ app:list | grep -A 2 oidc
```

Expected output:
```
  - oidc: 1.10.0 (enabled)
```

### Verify App Paths Configuration

Verify that `/opt/apps` is registered as an additional app directory:

```bash
# Check configured app paths
docker compose exec app php occ config:system:get apps_paths

# Verify the mount is accessible
docker compose exec app ls -la /opt/apps/oidc/

# Verify custom_apps is writable (for normal app installation)
docker compose exec -u www-data app touch /var/www/html/custom_apps/.test && echo "✅ custom_apps is writable" || echo "❌ custom_apps NOT writable"
docker compose exec app rm -f /var/www/html/custom_apps/.test
```

Expected: Output should show multiple app paths including index 2 (/opt/apps).

### Verify Consent Files

```bash
# Check controller exists in mounted location
docker compose exec app ls -la /opt/apps/oidc/lib/Controller/ConsentController.php

# Check Vue component exists
docker compose exec app ls -la /opt/apps/oidc/src/Consent.vue

# Check built JS exists
docker compose exec app ls -lh /opt/apps/oidc/js/oidc-consent.js
```

### Verify Database Migration

**Note**: These checks will only pass after restarting containers with the mounted OIDC app.

```bash
# Check if consent table exists
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "SHOW TABLES LIKE 'oc_oidc_user_consents';"

# Check table structure
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "DESCRIBE oc_oidc_user_consents;"

# Verify migration 0015 was applied
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "SELECT app, version FROM oc_migrations WHERE app = 'oidc' AND version LIKE '%0015%';"
```

Expected table structure:
- id: int(10) unsigned, auto_increment, primary key
- user_id: varchar(256), not null
- client_id: int(10) unsigned, not null
- scopes_granted: varchar(512), not null
- created_at: int(10) unsigned, not null
- updated_at: int(10) unsigned, not null
- expires_at: int(10) unsigned, nullable

### Verify Routes

```bash
docker compose exec app php occ router:list | grep consent
```

Expected output:
```
oidc.Consent.show        GET    apps/oidc/consent
oidc.Consent.grant       POST   apps/oidc/consent/grant
oidc.Consent.deny        POST   apps/oidc/consent/deny
```

## Testing the Consent Flow

### 1. Create an OAuth Client

The JWT client is automatically created by the post-installation hooks:

```bash
# Check if JWT client exists
docker compose exec app cat /var/www/html/.oauth-jwt/nextcloud_oauth_client.json
```

### 2. Initiate Authorization Flow

You can test using the MCP OAuth container or manually:

**Option A: Using MCP OAuth container**
```bash
# The mcp-oauth container will trigger the OAuth flow
docker compose logs -f mcp-oauth
```

**Option B: Manual browser test**
1. Get client_id from the JWT client JSON
2. Visit in browser:
```
http://localhost:8080/apps/oidc/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost:8001/oauth/callback&scope=openid+profile+email+mcp:notes:read+mcp:notes:write&state=test123
```

### 3. Expected Behavior

**First Authorization:**
1. User logs in (if not already authenticated)
2. **Consent screen appears** with:
   - Application name: "Nextcloud MCP Server JWT"
   - List of requested scopes with descriptions:
     - ✓ Basic authentication (openid) - required, cannot deselect
     - ✓ Profile information (profile)
     - ✓ Email address (email)
     - ✓ mcp:notes:read (custom scope, shown as-is)
     - ✓ mcp:notes:write (custom scope, shown as-is)
   - "Allow" and "Deny" buttons
3. User selects scopes and clicks "Allow"
4. Authorization proceeds with selected scopes
5. Consent is stored in database

**Subsequent Authorizations:**
- Same scopes → No consent screen (uses stored consent)
- Different scopes → Consent screen appears again
- If user clicks "Deny" → Returns `error=access_denied` to client

### 4. Verify Consent Stored

After granting consent:

```bash
# View all stored consents with formatted timestamps
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "
SELECT
    user_id,
    client_id,
    scopes_granted,
    FROM_UNIXTIME(created_at) as created,
    FROM_UNIXTIME(updated_at) as updated,
    FROM_UNIXTIME(expires_at) as expires
FROM oc_oidc_user_consents;
" nextcloud

# Or for a compact view:
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "SELECT * FROM oc_oidc_user_consents;" nextcloud
```

## Troubleshooting

### Consent Screen Not Appearing

**Check browser console** (F12 → Console tab):
```
# Look for JS errors like:
Failed to load resource: js/oidc-consent.js
```

**Check Nextcloud logs:**
```bash
docker compose exec app tail -f /var/www/html/data/nextcloud.log | grep -i consent
```

**Verify JS file loaded:**
```bash
# Check file exists and has correct size (~73KB)
docker compose exec app ls -lh /opt/apps/oidc/js/oidc-consent.js
```

**Clear Nextcloud caches:**
```bash
docker compose exec app php occ maintenance:repair
docker compose restart app
```

### Migration Didn't Run

**Check which migrations have been applied:**
```bash
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "SELECT app, version FROM oc_migrations WHERE app = 'oidc' ORDER BY version;" nextcloud
```

Expected to see `Version0015Date20251123100100` in the list.

**Manually trigger migrations:**
```bash
# Disable and re-enable app (triggers all pending migrations)
docker compose exec app php occ app:disable oidc
docker compose exec app php occ app:enable oidc

# Verify migration 0015 was applied
docker compose exec -T db mariadb -u nextcloud -ppassword nextcloud -e "SELECT version FROM oc_migrations WHERE app = 'oidc' AND version LIKE '%0015%';" nextcloud
```

### Routes Not Registered

If `router:list` doesn't show consent routes:

```bash
# The autoloader might not have picked up new classes
# Restart the container
docker compose restart app

# Wait for it to be ready
sleep 10

# Try again
docker compose exec app php occ router:list | grep consent
```

If still not working, check if ConsentController is accessible:
```bash
docker compose exec app php -r "
require_once '/var/www/html/lib/base.php';
\$class = 'OCA\\OIDCIdentityProvider\\Controller\\ConsentController';
if (class_exists(\$class)) {
    echo \"Class exists\n\";
} else {
    echo \"Class not found\n\";
}
"
```

## Making Changes

### Frontend Changes (Vue.js)

1. Edit source file on host:
```bash
cd ~/Software/oidc
# Edit src/Consent.vue
```

2. Rebuild JS:
```bash
npm run build
```

3. Refresh browser (container sees changes immediately via volume mount at /opt/apps/oidc)

### Backend Changes (PHP)

1. Edit files on host:
```bash
cd ~/Software/oidc
# Edit lib/Controller/ConsentController.php or other PHP files
```

2. Changes are immediately visible (PHP is interpreted, no build step)

3. For new classes or major changes, restart container:
```bash
docker compose restart app
```

### Database Schema Changes

If you modify the migration:

```bash
# Changes won't be picked up if migration already ran
# Need to recreate the database:
docker compose down -v  # Removes volumes
docker compose up -d    # Fresh start with clean DB
```

## Cleanup

### Reset Everything

```bash
cd ~/Projects/nextcloud-mcp-server
docker compose down -v
```

This removes:
- All containers
- Database volume (all data)
- OAuth client credentials

### Keep Data, Restart App

```bash
docker compose restart app
```

This preserves:
- Database (consents, clients, users)
- OAuth client credentials

## Development Workflow Summary

1. **Make changes** in `~/Software/oidc`
2. **Build JS** if you changed Vue files: `npm run build`
3. **Test immediately** - refresh browser or restart container
4. **No need** to rebuild Docker images or reinstall app
5. **Iterate quickly** with instant feedback

## Production Deployment

When ready to deploy:

1. **Create patch file** (already done):
   ```bash
   cd ~/Software/oidc
   git format-patch master --stdout > user-consent-feature.patch
   ```

2. **Test patch** in clean environment:
   ```bash
   # In a production-like environment
   cd /path/to/production/oidc
   git apply user-consent-feature.patch
   npm install
   npm run build
   php occ app:disable oidc
   php occ app:enable oidc
   ```

3. **Verify migration** runs automatically on app enable

4. **Submit pull request** to upstream repository
