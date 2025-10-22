#!/bin/bash
set -e

echo "=== JWT OAuth Client Setup ==="
echo "Installing and configuring OIDC app for JWT tokens..."

# Wait for Nextcloud to be fully initialized
sleep 5

# Install OIDC app if not already installed
if ! php /var/www/html/occ app:list | grep -q "oidc"; then
    echo "Installing OIDC app..."
    php /var/www/html/occ app:install oidc
else
    echo "OIDC app already installed"
fi

# Enable the app
php /var/www/html/occ app:enable oidc

# Check if JWT client already exists
# Use a location that www-data user owns
CLIENT_DIR="/var/www/html/.oauth-jwt"
CLIENT_FILE="$CLIENT_DIR/nextcloud_oauth_client.json"

if [ -f "$CLIENT_FILE" ]; then
    echo "JWT OAuth client already exists at $CLIENT_FILE"
    exit 0
fi

# Create directory owned by www-data
mkdir -p "$CLIENT_DIR"

# Create JWT OAuth client with proper scopes
echo "Creating JWT OAuth client..."

# The redirect URI for the MCP server
REDIRECT_URI="http://127.0.0.1:8002/oauth/callback"

# Create the client with JWT token type
OUTPUT=$(php /var/www/html/occ oidc:create \
    --token_type=jwt \
    --allowed_scopes="openid profile email nc:read nc:write" \
    "Nextcloud MCP Server JWT" \
    "$REDIRECT_URI")

echo "Client creation output:"
echo "$OUTPUT"

# Parse the JSON output to extract client_id, client_secret, and issued_at
# Output format is JSON
CLIENT_ID=$(echo "$OUTPUT" | grep '"client_id"' | sed 's/.*"client_id": "\([^"]*\)".*/\1/')
CLIENT_SECRET=$(echo "$OUTPUT" | grep '"client_secret"' | sed 's/.*"client_secret": "\([^"]*\)".*/\1/')
ISSUED_AT=$(echo "$OUTPUT" | grep '"issued_at"' | sed 's/.*"issued_at": \([0-9]*\).*/\1/')

if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
    echo "ERROR: Failed to parse client credentials from output"
    echo "Output was: $OUTPUT"
    exit 1
fi

# Use issued_at if available, otherwise use current timestamp
if [ -z "$ISSUED_AT" ]; then
    ISSUED_AT=$(date +%s)
fi

# Set expiration to 10 years in the future (JWT clients don't expire like DCR clients)
EXPIRES_AT=$((ISSUED_AT + 315360000))

echo "Successfully created JWT client:"
echo "  Client ID: ${CLIENT_ID:0:16}..."
echo "  Client Secret: [hidden]"

# Create the credentials file in the format expected by the MCP server
# This matches the format from ClientInfo.to_dict() in client_registration.py
cat > "$CLIENT_FILE" << EOF
{
    "client_id": "$CLIENT_ID",
    "client_secret": "$CLIENT_SECRET",
    "client_id_issued_at": $ISSUED_AT,
    "client_secret_expires_at": $EXPIRES_AT,
    "redirect_uris": ["$REDIRECT_URI"]
}
EOF

echo "Credentials saved to $CLIENT_FILE"

# Also save to environment variable format for easy access
cat > "$CLIENT_DIR/client_env.sh" << EOF
export NEXTCLOUD_OIDC_CLIENT_ID="$CLIENT_ID"
export NEXTCLOUD_OIDC_CLIENT_SECRET="$CLIENT_SECRET"
export NEXTCLOUD_OIDC_SCOPES="openid profile email nc:read nc:write"
EOF

chmod 600 "$CLIENT_DIR/client_env.sh"

echo "=== JWT OAuth Client Setup Complete ==="
echo "Client credentials are available at:"
echo "  JSON: $CLIENT_FILE"
echo "  ENV:  $CLIENT_DIR/client_env.sh"
