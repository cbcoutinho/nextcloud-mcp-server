#!/bin/bash
# Static OIDC client for the mcp-login-flow dev/test service (port 8004).
#
# The service used to register its own client via DCR. From oidc 2.5.0 an RFC
# 8707 resource is accepted only if the admin approved it for the client, which
# happens automatically for a static client's own resource_url and never for a
# DCR client -- and the server falls back to its client's resource_url on every
# authorize, so a DCR-registered server gets `invalid_target`. A static client is
# also what docs/login-flow-v2.md recommends for real deployments.
#
# Opt-in: runs only when LOGIN_FLOW_OIDC_CLIENT_ID and _SECRET are set (CI
# generates them; see .github/workflows/test.yml). The same values reach
# mcp-login-flow as NEXTCLOUD_OIDC_CLIENT_ID/_SECRET. Without them the service
# falls back to DCR, which works on oidc < 2.5.0 only.

set -e

if [ -z "${LOGIN_FLOW_OIDC_CLIENT_ID:-}" ] || [ -z "${LOGIN_FLOW_OIDC_CLIENT_SECRET:-}" ]; then
  echo "LOGIN_FLOW_OIDC_CLIENT_ID/_SECRET not set, skipping the static mcp-login-flow client"
  exit 0
fi

if ! php occ app:list --output=json 2>/dev/null | php -r 'exit(isset(json_decode(file_get_contents("php://stdin"),true)["enabled"]["oidc"]) ? 0 : 1);'; then
  echo "OIDC app not enabled, skipping the static mcp-login-flow client"
  exit 0
fi

SERVER_URL="${LOGIN_FLOW_MCP_SERVER_URL:-http://localhost:8004}"

# Recreated on every start so a changed secret or URL always takes effect.
php occ oidc:remove "$LOGIN_FLOW_OIDC_CLIENT_ID" >/dev/null 2>&1 || true
php occ oidc:create "Nextcloud MCP Server (login-flow, dev)" \
  "${SERVER_URL}/oauth/callback" \
  --client_id "$LOGIN_FLOW_OIDC_CLIENT_ID" \
  --client_secret "$LOGIN_FLOW_OIDC_CLIENT_SECRET" \
  --type confidential \
  --flow code \
  --resource_url "${SERVER_URL}/mcp" >/dev/null

echo "Static mcp-login-flow OIDC client ready: ${LOGIN_FLOW_OIDC_CLIENT_ID} (resource ${SERVER_URL}/mcp)"
