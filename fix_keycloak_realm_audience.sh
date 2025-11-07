#!/bin/bash

echo "Applying audience fix to Keycloak realm for ALL clients..."

# Get admin token
ADMIN_TOKEN=$(curl -s -X POST "http://localhost:8888/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=admin-cli" \
  -d "username=admin" \
  -d "password=admin" | jq -r '.access_token')

if [ -z "$ADMIN_TOKEN" ] || [ "$ADMIN_TOKEN" == "null" ]; then
  echo "Failed to get admin token. Is Keycloak running?"
  exit 1
fi

echo "Got admin token"

# Create a default client scope with audience mapper that will apply to ALL clients
echo "Creating default audience scope..."

# First, delete if it exists
curl -s -X DELETE "http://localhost:8888/admin/realms/nextcloud-mcp/client-scopes/default-audience" \
  -H "Authorization: Bearer $ADMIN_TOKEN" 2>/dev/null

# Create new client scope
SCOPE_RESPONSE=$(curl -s -X POST "http://localhost:8888/admin/realms/nextcloud-mcp/client-scopes" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "default-audience",
    "protocol": "openid-connect",
    "attributes": {
      "include.in.token.scope": "false",
      "display.on.consent.screen": "false"
    },
    "protocolMappers": [
      {
        "name": "mcp-server-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "consentRequired": false,
        "config": {
          "included.client.audience": "nextcloud-mcp-server",
          "access.token.claim": "true",
          "id.token.claim": "false"
        }
      },
      {
        "name": "mcp-url-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "consentRequired": false,
        "config": {
          "included.custom.audience": "http://localhost:8002",
          "access.token.claim": "true",
          "id.token.claim": "false"
        }
      }
    ]
  }')

# Get the scope ID
SCOPE_ID=$(curl -s -X GET "http://localhost:8888/admin/realms/nextcloud-mcp/client-scopes" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.[] | select(.name == "default-audience") | .id')

if [ -z "$SCOPE_ID" ] || [ "$SCOPE_ID" == "null" ]; then
  echo "Failed to create client scope"
  exit 1
fi

echo "Created client scope with ID: $SCOPE_ID"

# Make this a default client scope (applies to ALL clients automatically)
curl -s -X PUT "http://localhost:8888/admin/realms/nextcloud-mcp/default-default-client-scopes/$SCOPE_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

echo "Made it a default client scope"

# Now update ALL existing clients to use this scope
echo "Updating existing clients..."

# Get all clients
CLIENTS=$(curl -s -X GET "http://localhost:8888/admin/realms/nextcloud-mcp/clients" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.[] | select(.clientId != "admin-cli" and .clientId != "account" and .clientId != "broker" and .clientId != "realm-management" and .clientId != "security-admin-console" and .clientId != "account-console") | .id')

for CLIENT_ID in $CLIENTS; do
  CLIENT_NAME=$(curl -s -X GET "http://localhost:8888/admin/realms/nextcloud-mcp/clients/$CLIENT_ID" \
    -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.clientId')

  echo "  Adding scope to client: $CLIENT_NAME"

  # Add the default scope to this client
  curl -s -X PUT "http://localhost:8888/admin/realms/nextcloud-mcp/clients/$CLIENT_ID/default-client-scopes/$SCOPE_ID" \
    -H "Authorization: Bearer $ADMIN_TOKEN"
done

echo ""
echo "Testing with a new token..."
TOKEN=$(curl -s -X POST 'http://localhost:8888/realms/nextcloud-mcp/protocol/openid-connect/token' \
  -d 'grant_type=password' \
  -d 'client_id=nextcloud-mcp-server' \
  -d 'client_secret=mcp-secret-change-in-production' \
  -d 'username=admin' \
  -d 'password=admin' | jq -r '.access_token')

echo "Token audience:"
echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('aud:', d.get('aud', 'NO AUD'))"

echo ""
echo "✅ Audience configuration applied to ALL clients in the realm!"
echo "New clients registered by Gemini will automatically get these audiences."