#!/bin/bash
# Configure Astrolabe OAuth client for MCP server integration
# Creates an OIDC client in Nextcloud and stores credentials in config.php
# so the "Authorize via OAuth" button in Astrolabe settings works.

set -e

# Check MCP_SERVER_URL env var, fall back to config.php value
MCP_SERVER_URL="${MCP_SERVER_URL:-$(php occ config:system:get mcp_server_url 2>/dev/null || true)}"

if [ -z "$MCP_SERVER_URL" ]; then
  echo "MCP_SERVER_URL not set and mcp_server_url not in config.php, skipping Astrolabe OAuth setup"
  exit 0
fi

# Skip if client already configured
EXISTING_CLIENT_ID=$(php occ config:system:get astrolabe_client_id 2>/dev/null || true)
if [ -n "$EXISTING_CLIENT_ID" ]; then
  echo "Astrolabe OAuth client already configured: $EXISTING_CLIENT_ID"
  exit 0
fi

# Check if OIDC app is enabled (required for oidc:create)
if ! php occ app:list --output=json 2>/dev/null | php -r 'exit(isset(json_decode(file_get_contents("php://stdin"),true)["enabled"]["oidc"]) ? 0 : 1);'; then
  echo "OIDC app not enabled, skipping Astrolabe OAuth setup"
  exit 0
fi

echo "Creating Astrolabe OAuth client..."

# Determine public MCP server URL (for token audience / resource indicator)
MCP_PUBLIC_URL="${MCP_SERVER_PUBLIC_URL:-$MCP_SERVER_URL}"

# Get Nextcloud external URL for redirect URI
NC_EXTERNAL_URL=$(php occ config:system:get overwrite.cli.url 2>/dev/null || echo "http://localhost:8080")
NC_EXTERNAL_URL="${NC_EXTERNAL_URL%/}"

# Client ID must be 32-64 chars, A-Za-z0-9
CLIENT_ID="astrolabeMcpClientOAuth00000000000"
REDIRECT_URI="${NC_EXTERNAL_URL}/apps/astrolabe/oauth/callback"

# All scopes the MCP server supports (must match DCR scopes in app.py)
ALLOWED_SCOPES="openid profile email offline_access notes:read notes:write calendar:read calendar:write todo:read todo:write contacts:read contacts:write cookbook:read cookbook:write deck:read deck:write tables:read tables:write files:read files:write sharing:read sharing:write news:read news:write collectives:read collectives:write semantic:read"

# Create OAuth client
CLIENT_JSON=$(php occ oidc:create "Astrolabe" \
  "$REDIRECT_URI" \
  --client_id "$CLIENT_ID" \
  --type confidential \
  --flow code \
  --token_type jwt \
  --resource_url "$MCP_PUBLIC_URL" \
  --allowed_scopes "$ALLOWED_SCOPES")

# Extract client_secret from JSON output
CLIENT_SECRET=$(echo "$CLIENT_JSON" | php -r '$d=json_decode(file_get_contents("php://stdin")); echo $d->client_secret ?? "";')

if [ -z "$CLIENT_SECRET" ]; then
  echo "ERROR: Failed to extract client_secret from oidc:create output"
  echo "Output was: $CLIENT_JSON"
  exit 1
fi

# Store credentials in config.php
php occ config:system:set astrolabe_client_id --value="$CLIENT_ID"
php occ config:system:set astrolabe_client_secret --value="$CLIENT_SECRET"
php occ config:system:set mcp_server_public_url --value="$MCP_PUBLIC_URL"

echo "Astrolabe OAuth client configured successfully"
echo "  Client ID: $CLIENT_ID"
echo "  Redirect URI: $REDIRECT_URI"
echo "  MCP Server Public URL: $MCP_PUBLIC_URL"
