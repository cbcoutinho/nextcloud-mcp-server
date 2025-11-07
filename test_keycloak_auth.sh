#!/bin/bash

echo "Getting token from Keycloak..."
TOKEN=$(curl -s -X POST 'http://localhost:8888/realms/nextcloud-mcp/protocol/openid-connect/token' \
  -d 'grant_type=password' \
  -d 'client_id=nextcloud-mcp-server' \
  -d 'client_secret=mcp-secret-change-in-production' \
  -d 'username=admin' \
  -d 'password=admin' | jq -r '.access_token')

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo "Failed to get token from Keycloak"
  exit 1
fi

echo "Token obtained successfully"
echo ""
echo "Token audience claim:"
echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('aud:', d.get('aud', 'NO AUD FIELD'))"

echo ""
echo "Testing MCP endpoint at http://localhost:8002/mcp..."
RESPONSE=$(curl -s -X POST http://localhost:8002/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "initialize", "params": {"protocolVersion": "1.0", "capabilities": {}}, "id": 1}')

echo "Response:"
echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"

# Check if authentication succeeded
if echo "$RESPONSE" | grep -q '"result"'; then
  echo ""
  echo "✅ Authentication successful! Keycloak is working with the MCP server."
else
  echo ""
  echo "❌ Authentication failed. Checking logs..."
  docker compose logs mcp-keycloak --tail 5
fi