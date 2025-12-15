#!/bin/bash

set -euox pipefail

echo "Installing and configuring Astrolabe app for testing..."

# Check if development astrolabe app is mounted at /opt/apps/astrolabe
if [ -d /opt/apps/astrolabe ]; then
    echo "Development astrolabe app found at /opt/apps/astrolabe"

    # Remove any existing astrolabe app in custom_apps (from app store or old symlink)
    if [ -e /var/www/html/custom_apps/astrolabe ]; then
        echo "Removing existing astrolabe in custom_apps..."
        rm -rf /var/www/html/custom_apps/astrolabe
    fi

    # Create symlink from custom_apps to the mounted development version
    # Per Nextcloud docs: apps outside server root need symlinks in server root
    echo "Creating symlink: custom_apps/astrolabe -> /opt/apps/astrolabe"
    ln -sf /opt/apps/astrolabe /var/www/html/custom_apps/astrolabe

    echo "Enabling astrolabe app from /opt/apps (development mode via symlink)"
    php /var/www/html/occ app:enable astrolabe
elif [ -d /var/www/html/custom_apps/astrolabe ]; then
    echo "astrolabe app directory found in custom_apps (already installed)"
    php /var/www/html/occ app:enable astrolabe
else
    echo "astrolabe app not found, installing from app store..."
    php /var/www/html/occ app:install astrolabe
    php /var/www/html/occ app:enable astrolabe
fi

# Configure MCP server URLs in Nextcloud system config
# - mcp_server_url: Internal URL for PHP app to call MCP server APIs (Docker internal network)
# - mcp_server_public_url: Public URL for OAuth token audience (what browsers/MCP clients see)
php /var/www/html/occ config:system:set mcp_server_url --value='http://mcp-oauth:8001'
php /var/www/html/occ config:system:set mcp_server_public_url --value='http://localhost:8001'

# Create OAuth client for Astrolabe app
# The resource_url MUST match what the MCP server expects as token audience
# This allows tokens from this client to be validated by MCP server's UnifiedTokenVerifier
MCP_CLIENT_ID="nextcloudMcpServerUIPublicClient"
MCP_RESOURCE_URL="http://localhost:8001"
MCP_REDIRECT_URI="http://localhost:8080/apps/astrolabe/oauth/callback"

echo "Configuring OAuth client for Astrolabe..."

# Check if client already exists
if php /var/www/html/occ oidc:list 2>/dev/null | grep -q "$MCP_CLIENT_ID"; then
    echo "OAuth client $MCP_CLIENT_ID already exists, removing to recreate with correct settings..."
    php /var/www/html/occ oidc:remove "$MCP_CLIENT_ID" || true
fi

# Create OAuth client with correct resource_url for MCP server audience
echo "Creating OAuth confidential client with resource_url=$MCP_RESOURCE_URL"
CLIENT_OUTPUT=$(php /var/www/html/occ oidc:create \
    "Astrolabe" \
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
    echo "Configuring Astrolabe client secret in system config..."
    php /var/www/html/occ config:system:set astrolabe_client_secret --value="$CLIENT_SECRET"
    echo "✓ Client secret configured: ${CLIENT_SECRET:0:8}..."
else
    echo "⚠ Warning: Could not extract client_secret from OIDC client creation"
fi

# Configure OAuth client ID in system config
echo "Configuring Astrolabe client ID in system config..."
php /var/www/html/occ config:system:set astrolabe_client_id --value="$MCP_CLIENT_ID"
echo "✓ Client ID configured: $MCP_CLIENT_ID"

echo "Astrolabe app installed and configured successfully"
