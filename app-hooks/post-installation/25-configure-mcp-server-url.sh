#!/bin/bash
# Configure MCP server URL for Astrolabe background sync
# This URL is used by Astrolabe to send app passwords to the MCP server, and to
# deliver sync events (its webhooks) to the server's /webhooks/nextcloud.
#
# The before-starting copy re-applies this on every app start; to apply it
# without a restart, run it by hand:
#   docker compose exec -u www-data -e MCP_SERVER_URL=http://mcp:8000 app \
#     bash /docker-entrypoint-hooks.d/post-installation/25-configure-mcp-server-url.sh

set -e

if [ -z "${MCP_SERVER_URL:-}" ]; then
  echo "MCP_SERVER_URL not set, skipping Astrolabe MCP server URL configuration"
  exit 0
fi

echo "Configuring MCP server URL: $MCP_SERVER_URL"

# Set the mcp_server_url in config.php via occ
php occ config:system:set mcp_server_url --value="$MCP_SERVER_URL"

# Sync-event delivery: the shared secret the MCP ingress checks, and the presets
# whose events Astrolabe forwards (none are enabled by default). The
# astrolabe-sync-worker compose service runs the queued deliveries.
if [ -n "${WEBHOOK_SECRET:-}" ]; then
  php occ config:system:set mcp_webhook_secret --value="$WEBHOOK_SECRET"
  php occ config:app:set astrolabe enabled_sync_presets \
    --value='["notes_sync","files_sync","deck_sync"]'
  echo "Astrolabe sync-event delivery enabled"
else
  echo "WEBHOOK_SECRET not set: Astrolabe sync-event delivery left off"
fi

echo "MCP server URL configured successfully"
