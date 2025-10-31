#!/bin/bash
#
# Configure user_oidc to accept bearer tokens from Keycloak
#
# This script sets up Keycloak as an external OIDC provider for Nextcloud.
# It enables bearer token validation, allowing the MCP server to use Keycloak
# tokens to access Nextcloud APIs without admin credentials.
#

set -e

echo "===================================================================="
echo "Configuring user_oidc provider for Keycloak..."
echo "===================================================================="

# Wait for Keycloak to be ready and realm to be available
echo "Waiting for Keycloak realm to be available..."
MAX_RETRIES=30
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -sf http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration > /dev/null 2>&1; then
        echo "✓ Keycloak realm is ready"
        break
    fi
    echo "  Waiting for Keycloak... (attempt $((RETRY_COUNT + 1))/$MAX_RETRIES)"
    sleep 5
    RETRY_COUNT=$((RETRY_COUNT + 1))
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "⚠ Warning: Keycloak not available after $MAX_RETRIES attempts"
    echo "  Keycloak provider will not be configured"
    echo "  You can configure it manually using:"
    echo "  docker compose exec app php occ user_oidc:provider keycloak \\"
    echo "    --clientid='nextcloud' \\"
    echo "    --clientsecret='nextcloud-secret-change-in-production' \\"
    echo "    --discoveryuri='http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration' \\"
    echo "    --check-bearer=1 \\"
    echo "    --bearer-provisioning=1 \\"
    echo "    --unique-uid=1"
    exit 0
fi

# Check if provider already exists
if php /var/www/html/occ user_oidc:provider keycloak 2>/dev/null | grep -q "Identifier"; then
    echo "  Keycloak provider already exists, updating configuration..."

    # Update existing provider
    php /var/www/html/occ user_oidc:provider keycloak \
        --clientid="nextcloud" \
        --clientsecret="nextcloud-secret-change-in-production" \
        --discoveryuri="http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration" \
        --check-bearer=1 \
        --bearer-provisioning=1 \
        --unique-uid=1 \
        --mapping-uid="sub" \
        --mapping-display-name="name" \
        --mapping-email="email" \
        --scope="openid profile email offline_access"

    echo "✓ Updated Keycloak provider configuration"
else
    echo "  Creating new Keycloak provider..."

    # Create new provider
    php /var/www/html/occ user_oidc:provider keycloak \
        --clientid="nextcloud" \
        --clientsecret="nextcloud-secret-change-in-production" \
        --discoveryuri="http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration" \
        --check-bearer=1 \
        --bearer-provisioning=1 \
        --unique-uid=1 \
        --mapping-uid="sub" \
        --mapping-display-name="name" \
        --mapping-email="email" \
        --scope="openid profile email offline_access"

    echo "✓ Created Keycloak provider"
fi

# Display provider details
echo ""
echo "Keycloak provider configuration:"
php /var/www/html/occ user_oidc:provider keycloak

echo ""
echo "===================================================================="
echo "✓ Keycloak provider configured successfully"
echo "===================================================================="
echo ""
echo "Key features enabled:"
echo "  • Bearer token validation (--check-bearer=1)"
echo "  • Automatic user provisioning (--bearer-provisioning=1)"
echo "  • Unique user IDs (--unique-uid=1)"
echo "  • Offline access scope (for refresh tokens)"
echo ""
echo "MCP server can now use Keycloak tokens to access Nextcloud APIs"
echo "without admin credentials (ADR-002 architecture)."
echo ""
