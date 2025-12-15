#!/bin/bash

set -euox pipefail

echo "Installing and configuring Astroglobe app for testing..."

# Check if development astroglobe app is mounted at /opt/apps/astroglobe
if [ -d /opt/apps/astroglobe ]; then
    echo "Development astroglobe app found at /opt/apps/astroglobe"

    # Remove any existing astroglobe app in custom_apps (from app store or old symlink)
    if [ -e /var/www/html/custom_apps/astroglobe ]; then
        echo "Removing existing astroglobe in custom_apps..."
        rm -rf /var/www/html/custom_apps/astroglobe
    fi

    # Create symlink from custom_apps to the mounted development version
    # Per Nextcloud docs: apps outside server root need symlinks in server root
    echo "Creating symlink: custom_apps/astroglobe -> /opt/apps/astroglobe"
    ln -sf /opt/apps/astroglobe /var/www/html/custom_apps/astroglobe

    echo "Enabling astroglobe app from /opt/apps (development mode via symlink)"
    php /var/www/html/occ app:enable astroglobe
elif [ -d /var/www/html/custom_apps/astroglobe ]; then
    echo "astroglobe app directory found in custom_apps (already installed)"
    php /var/www/html/occ app:enable astroglobe
else
    echo "astroglobe app not found, installing from app store..."
    php /var/www/html/occ app:install astroglobe
    php /var/www/html/occ app:enable astroglobe
fi

# Configure MCP server URLs in Nextcloud system config
# - mcp_server_url: Internal URL for PHP app to call MCP server APIs (Docker internal network)
# - mcp_server_public_url: Public URL for OAuth token audience (what browsers/MCP clients see)
php /var/www/html/occ config:system:set mcp_server_url --value='http://mcp-oauth:8001'
php /var/www/html/occ config:system:set mcp_server_public_url --value='http://localhost:8001'

# Create OAuth client for Astroglobe app
# The resource_url MUST match what the MCP server expects as token audience
# This allows tokens from this client to be validated by MCP server's UnifiedTokenVerifier
MCP_CLIENT_ID="nextcloudMcpServerUIPublicClient"
MCP_RESOURCE_URL="http://localhost:8001"
MCP_REDIRECT_URI="http://localhost:8080/apps/astroglobe/oauth/callback"

echo "Configuring OAuth client for Astroglobe..."

# Check if client already exists
if php /var/www/html/occ oidc:list 2>/dev/null | grep -q "$MCP_CLIENT_ID"; then
    echo "OAuth client $MCP_CLIENT_ID already exists, removing to recreate with correct settings..."
    php /var/www/html/occ oidc:remove "$MCP_CLIENT_ID" || true
fi

# Create OAuth client with correct resource_url for MCP server audience
echo "Creating OAuth confidential client with resource_url=$MCP_RESOURCE_URL"
CLIENT_OUTPUT=$(php /var/www/html/occ oidc:create \
    "Astroglobe" \
    "$MCP_REDIRECT_URI" \
    --client_id="$MCP_CLIENT_ID" \
    --type=confidential \
    --flow=code \
    --token_type=jwt \
    --resource_url="$MCP_RESOURCE_URL" \
    --allowed_scopes="openid profile email offline_access notes:read notes:write calendar:read calendar:write contacts:read contacts:write cookbook:read cookbook:write deck:read deck:write tables:read tables:write files:read files:write")

echo "$CLIENT_OUTPUT"

# Extract client_secret from JSON output
CLIENT_SECRET=$(echo "$CLIENT_OUTPUT" | php -r 'echo json_decode(file_get_contents("php://stdin"), true)["client_secret"] ?? "";')

if [ -n "$CLIENT_SECRET" ]; then
    echo "Configuring Astroglobe client secret in system config..."
    php /var/www/html/occ config:system:set astroglobe_client_secret --value="$CLIENT_SECRET"
    echo "✓ Client secret configured: ${CLIENT_SECRET:0:8}..."
else
    echo "⚠ Warning: Could not extract client_secret from OIDC client creation"
fi

echo "Astroglobe app installed and configured successfully"
