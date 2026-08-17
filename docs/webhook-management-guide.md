# Webhook Management Guide

This guide explains how to enable and disable webhook-based vector sync.
Webhooks give near-real-time synchronization of content changes to the vector
database, complementing the default polling-based sync.

Change events are delivered by the
**[Astrolabe app](https://github.com/cbcoutinho/astrolabe)**: it subscribes to
Nextcloud's events and POSTs each one to the MCP server's ingress at
`/webhooks/nextcloud`. There is nothing to register on either side and no occ
command for adding or removing listeners — delivery is configured entirely from
Astrolabe's admin settings (equivalently, `occ config:system:set`).

**Related ADRs:**
- ADR-010: Webhook-Based Vector Sync
- ADR-020: Deployment Modes and Configuration Validation

## Prerequisites

1. **MCP server** reachable from Nextcloud over HTTP(S), with
   `VECTOR_SYNC_ENABLED=true` and `WEBHOOK_SECRET` set
2. **Astrolabe app** installed in Nextcloud (Nextcloud 30+)
3. **Background sync credentials** provisioned per user — the receiver needs
   them to read the changed content back
4. **Nextcloud background jobs running** — delivery is enqueued as a job, so
   cron cadence is your delivery latency

## How It Works

Two pieces must both be in place:

1. **Change delivery** — Astrolabe's event listeners enqueue a background job
   that POSTs the change envelope to `{mcp_server_url}/webhooks/nextcloud` with
   `Authorization: Bearer <shared secret>`.
2. **Background sync credentials** — the MCP server reads the changed content
   back out of Nextcloud on behalf of that user (see below).

Webhooks are additive: the polling scanner keeps running and reconciles anything
not delivered (server down, job backlog, unsupported event).

## Setup

### Step 1: Configure the MCP server

```bash
VECTOR_SYNC_ENABLED=true
# generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
WEBHOOK_SECRET=<secret>
```

Without `WEBHOOK_SECRET` the `/webhooks/nextcloud` route is **not mounted**
(404) and vector sync falls back to polling only (GHSA-8vh3-g2qg-2h2c).

### Step 2: Point Astrolabe at the MCP server

In Nextcloud, **Administration settings → Astrolabe**:

- **MCP Server URL (internal)** — the address Nextcloud can reach the MCP server
  on
- **Webhook shared secret** — must equal the server's `WEBHOOK_SECRET`
  (write-only: the stored value is never displayed, and leaving the field blank
  keeps the current one)

Both are Nextcloud *system* config values, so they can equally be set from the
CLI:

```bash
php occ config:system:set mcp_server_url --value="https://mcp.example.com"
php occ config:system:set mcp_webhook_secret --value="<secret>"
```

Use an `https://` URL — the secret travels as a bearer token.

### Step 3: Enable sync presets

On the same admin page, toggle the presets you want. Presets are stored in app
config (`astrolabe.enabled_sync_presets`); with none enabled, no listener fires.

| Preset | Events delivered | What it keeps in sync |
|---|---|---|
| `notes_sync` | Node created/written/deleted under `…/files/Notes/` | Notes (`.md`) |
| `files_sync` | Node created/written/deleted anywhere, plus SystemTag changes (NC 32+) | Notes, tagged PDFs, and index-tag changes |
| `deck_sync` | Deck card created/updated/deleted, board updated | Deck cards (board updates are reconciled by the scanner) |

Every preset maps to a doc type vector sync actually indexes; the receiver
ignores anything else it is handed. Files are indexed only when they carry an
index tag (`vector-index` / `keyword-index`, directly or via a tagged folder), so
a file event is delivered as a *reconcile*: the server resolves the file's
current tag membership and then indexes or releases it. Calendar objects, Tables
rows and Forms submissions have no index and therefore no preset.

### Disabling

- **System-wide:** toggle the presets off, or flip the master switch
  (`php occ config:app:set astrolabe native_sync_enabled --value=false --type=boolean`).
- **Per user:** revoke that user's background sync credentials (Nextcloud →
  Personal settings → Astrolabe → *Revoke Access*). Events still arrive but
  cannot be processed for them.

## Background Sync Credentials

Delivery alone is not enough — the MCP server must read the changed content
back. How credentials are provisioned depends on deployment mode:

### Single-user BasicAuth

```bash
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=<app-password>
```

Nothing per-user to provision; background sync runs as that account.

### Multi-user BasicAuth / OAuth modes

```bash
MCP_DEPLOYMENT_MODE=multi_user_basic     # or the OAuth default
ENABLE_BACKGROUND_OPERATIONS=true
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/app/data/tokens.db
```

Each user provisions once, from **Nextcloud → Personal settings → Astrolabe**:

1. **Authorize via OAuth** — stores a refresh token in the MCP server's
   `tokens.db`, used for management API access.
2. **App password** — generate one under Nextcloud → Security and paste it into
   the Astrolabe panel. Stored encrypted in `oc_preferences` and used by the
   background scanners; needed because OAuth refresh tokens expire sooner.

Verify the panel shows "Background Sync Access: Active".

## Monitoring

```bash
# MCP server
docker compose logs mcp | grep -i webhook
#   "Queued document from webhook: ..."   → success
#   401 on /webhooks/nextcloud            → secret mismatch
#   "User X no longer provisioned"        → missing background credentials

# Nextcloud
docker compose exec app cat /var/www/html/data/nextcloud.log | \
  jq 'select(.app == "astrolabe")' | tail
```

```sql
-- Astrolabe sync settings (presets, master switch)
SELECT configkey, configvalue FROM oc_appconfig WHERE appid = 'astrolabe';

-- Per-user background sync credentials
SELECT userid, configkey FROM oc_preferences WHERE appid = 'astrolabe';
```

## Common Issues

### Webhooks never arrive
1. `WEBHOOK_SECRET` unset on the MCP server → the route returns 404. Set it and
   restart.
2. No presets enabled, or `mcp_server_url` unreachable from the Nextcloud
   container (`localhost` inside a container is not your laptop).
3. Background jobs not running — delivery is a queued job
   (`php occ background:job:worker`, or a working cron).
4. The change was to something vector sync doesn't index — an untagged file, a
   file type other than PDF, a calendar event (see the preset table).

### 401 on `/webhooks/nextcloud`
`mcp_webhook_secret` (Nextcloud) does not match `WEBHOOK_SECRET` (MCP server).

### "User X no longer provisioned, stopping scanner"
Background sync credentials missing or expired — re-provision from Personal
settings → Astrolabe.

### "Access forbidden - Your client is not authorized to connect"
OAuth client registration expired or absent; restart the MCP server to trigger
DCR re-registration and confirm `NEXTCLOUD_OIDC_CLIENT_ID` /
`NEXTCLOUD_OIDC_CLIENT_SECRET`.

## Security Considerations

- `WEBHOOK_SECRET` is **required** (GHSA-8vh3-g2qg-2h2c). The receiver trusts
  the `user.uid` in the payload and feeds it to Qdrant, so an unauthenticated
  POST could delete or re-index any user's embeddings.
- Serve the MCP server over HTTPS; the secret is a static bearer token.
- Refresh tokens and app passwords are encrypted with `TOKEN_ENCRYPTION_KEY` —
  keep it in a secrets manager, not in the image.
