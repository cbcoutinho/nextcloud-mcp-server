#!/bin/bash
set -e

echo "=== Testing Separate Clients Architecture ==="
echo ""

# Check both clients exist in Keycloak
echo "1. Verifying Keycloak clients..."
docker compose exec -T app curl -s http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration > /dev/null && echo "✓ Keycloak realm available"

# Check user_oidc provider configuration
echo ""
echo "2. Checking user_oidc provider..."
PROVIDER_INFO=$(docker compose exec -T app php occ user_oidc:provider keycloak)
echo "$PROVIDER_INFO" | grep -q "nextcloud" && echo "✓ user_oidc configured with 'nextcloud' client"

# Get token from nextcloud-mcp-server client
echo ""
echo "3. Getting token from 'nextcloud-mcp-server' client..."
TOKEN=$(curl -s -X POST "http://localhost:8888/realms/nextcloud-mcp/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=nextcloud-mcp-server" \
  -d "client_secret=mcp-secret-change-in-production" \
  -d "username=admin" \
  -d "password=admin" \
  -d "scope=openid profile email offline_access" | jq -r '.access_token')

if [ "$TOKEN" = "null" ] || [ -z "$TOKEN" ]; then
    echo "✗ Failed to get token"
    exit 1
fi

echo "✓ Got token from nextcloud-mcp-server client"

# Check token claims
echo ""
echo "4. Inspecting token claims..."
CLAIMS=$(echo "$TOKEN" | cut -d'.' -f2 | base64 -d 2>/dev/null | jq '{aud, azp, iss, preferred_username}')
echo "$CLAIMS"

AUD=$(echo "$CLAIMS" | jq -r '.aud')
AZP=$(echo "$CLAIMS" | jq -r '.azp')

echo ""
echo "Architecture validation:"
if [ "$AUD" = "nextcloud" ]; then
    echo "  ✓ aud='nextcloud' - Token intended for Nextcloud resource server"
else
    echo "  ✗ FAILED: aud='$AUD', expected 'nextcloud'"
    exit 1
fi

if [ "$AZP" = "nextcloud-mcp-server" ]; then
    echo "  ✓ azp='nextcloud-mcp-server' - Token requested by MCP Server client"
else
    echo "  ✗ FAILED: azp='$AZP', expected 'nextcloud-mcp-server'"
    exit 1
fi

# Test with Nextcloud API
echo ""
echo "5. Testing token with Nextcloud API..."
HTTP_CODE=$(curl -s -w "%{http_code}" -o /tmp/nc_response.json \
    -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8080/ocs/v2.php/cloud/capabilities?format=json")

echo "HTTP Status: $HTTP_CODE"

if [ "$HTTP_CODE" = "200" ]; then
    echo "✓ Token validated successfully!"
    echo ""
    echo "===================================================================="
    echo "SUCCESS: Separate Clients Architecture Working!"
    echo "===================================================================="
    echo ""
    echo "Summary:"
    echo "  - MCP Server client: 'nextcloud-mcp-server' (requests tokens)"
    echo "  - Resource server: 'nextcloud' (validates tokens via user_oidc)"
    echo "  - Token audience: 'nextcloud' (proper resource targeting)"
    echo "  - Token azp: 'nextcloud-mcp-server' (identifies requester)"
    echo ""
    echo "This architecture supports:"
    echo "  - Future multi-resource tokens: aud=['nextcloud', 'other-service']"
    echo "  - Clear separation of OAuth client vs resource server"
    echo "  - RFC 8707 Resource Indicators compliance"
else
    echo "✗ Token validation failed"
    cat /tmp/nc_response.json
    exit 1
fi
