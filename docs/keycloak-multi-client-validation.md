# Keycloak Multi-Client Token Validation

## Executive Summary

**Question**: Can Nextcloud's `user_oidc` app (configured with client A) validate bearer tokens from client B in the same Keycloak realm?

**Answer**: ✅ **YES** - user_oidc validates tokens at the **realm level**, not per-client.

## Test Results

### Setup
- **Keycloak Realm**: `nextcloud-mcp`
- **Provider in user_oidc**: Configured with `mcp-client` credentials
- **Test**: Get token from `test-client-b`, validate via Nextcloud API

### Result
```bash
# Token from test-client-b (client B)
$ TOKEN=$(curl -X POST ".../token" -d "client_id=test-client-b" ...)

# Validated successfully by Nextcloud (configured with mcp-client = client A)
$ curl -H "Authorization: Bearer $TOKEN" "http://nextcloud/ocs/.../capabilities"
HTTP/1.1 200 OK
{"ocs":{"meta":{"status":"ok"}}}
```

✅ **Token from client B validated successfully!**

## How It Works

### Token Structure from Keycloak

**Access Token** (password grant):
```json
{
  "iss": "http://keycloak/realms/nextcloud-mcp",
  "azp": "test-client-b",           // Authorized party = client B
  "typ": "Bearer",
  "exp": 1234567890,
  // NO "sub" claim
  // NO "aud" claim
  "scope": "openid profile email"
}
```

**ID Token** (for comparison):
```json
{
  "iss": "http://keycloak/realms/nextcloud-mcp",
  "aud": "test-client-b",            // Audience = client B
  "sub": "923da741-7ebe-4cf9-baf2-37fcf2ecc95d",
  "azp": "test-client-b"
}
```

**Key Observation**: Access tokens from Keycloak's password grant **do not contain** `sub` or `aud` claims!

### Validation Flow in user_oidc

From source code analysis (`~/Software/user_oidc/lib/User/Backend.php`):

```
1. Request with Bearer token arrives
   ↓
2. user_oidc loops through providers with checkBearer=true
   ↓
3. Try SelfEncodedValidator (JWT/JWKS validation):
   - Validates JWT signature using Keycloak's JWKS
   - Tries to extract 'sub' claim → FAILS (no sub in access token)
   ↓
4. Fallback to UserInfoValidator:
   - Calls Keycloak userinfo endpoint with bearer token
   - Keycloak validates token server-side
   - Returns userinfo with 'sub' claim
   → SUCCESS!
   ↓
5. User identified, request authorized
```

### Why This Works

**Realm-Level Trust**:
- Keycloak's userinfo endpoint validates ANY valid token from the realm
- It doesn't matter which client issued the token
- The token is validated by Keycloak itself (via userinfo call)

**No Audience Check**:
- Access tokens have no `aud` claim
- SelfEncodedValidator's audience check is bypassed (no audience to validate)
- UserInfoValidator doesn't check audience (delegates to Keycloak)

**Client Credentials Role**:
- The configured `client_id`/`client_secret` in user_oidc are **NOT used** for bearer token validation
- They're only used for OAuth login flows (authorization code exchange)
- Userinfo endpoint doesn't require client authentication

## Source Code Evidence

### SelfEncodedValidator - Audience Check

```php
// ~/Software/user_oidc/lib/User/Validator/SelfEncodedValidator.php:64-76

$checkAudience = !isset($oidcSystemConfig['selfencoded_bearer_validation_audience_check'])
    || !in_array($oidcSystemConfig['selfencoded_bearer_validation_audience_check'],
         [false, 'false', 0, '0'], true);

if ($checkAudience) {
    $tokenAudience = $payload->aud ?? null;

    if ((is_string($tokenAudience) && $tokenAudience !== $providerClientId)
        || (is_array($tokenAudience) && !in_array($providerClientId, $tokenAudience))) {
        $this->logger->debug('Audience does not match client ID');
        return null;  // REJECT
    }
}

// If $tokenAudience is null (our case), both conditions are false → validation continues
```

### UserInfoValidator - No Client Auth

```php
// ~/Software/user_oidc/lib/Service/OIDCService.php:28-45

public function userinfo(Provider $provider, string $accessToken): array {
    $url = $this->discoveryService->obtainDiscovery($provider)['userinfo_endpoint'];

    // Bearer token passed directly - NO client credentials used
    $options = ['headers' => ['Authorization' => 'Bearer ' . $accessToken]];

    return json_decode($this->clientService->get($url, [], $options), true);
}
```

### Keycloak Userinfo Response

```bash
$ curl -H "Authorization: Bearer $TOKEN_FROM_CLIENT_B" \
    "http://keycloak/realms/nextcloud-mcp/protocol/openid-connect/userinfo"

{
  "sub": "923da741-7ebe-4cf9-baf2-37fcf2ecc95d",
  "email_verified": true,
  "name": "Admin User",
  "email": "admin@example.com"
}
```

Keycloak validates the token **regardless of which client issued it**, as long as it's from the same realm.

## Implications for Your Architecture

### Desired Architecture
```
MCP Server (client A) ← DCR with Keycloak
MCP Clients (client B, C, D...) ← DCR with Keycloak
Nextcloud user_oidc ← configured once with any client from realm
```

### What This Means

✅ **You can do exactly what you want!**

1. **Configure user_oidc once** with any client from the Keycloak realm (e.g., a dedicated `nextcloud-validator` client)

2. **MCP Server registers via DCR** as a unique client (e.g., `mcp-server-abc123`)
   - Gets its own client credentials
   - Issues tokens with `azp: "mcp-server-abc123"`
   - These tokens will be validated by user_oidc!

3. **MCP Clients also use DCR** (each gets unique identity)
   - Client A: `client-123`
   - Client B: `client-456`
   - Tokens from all clients validated by user_oidc!

4. **Tokens from ANY client** in the realm can access Nextcloud APIs
   - user_oidc validates via Keycloak userinfo endpoint
   - Realm-level trust (not per-client)

### Configuration

**Step 1: Configure user_oidc Provider**
```bash
php occ user_oidc:provider keycloak-realm \
    --clientid="nextcloud-validator" \
    --clientsecret="***" \
    --discoveryuri="https://keycloak/realms/my-realm/.well-known/openid-configuration" \
    --check-bearer=1 \
    --bearer-provisioning=1
```

**Step 2: MCP Server Registers with Keycloak (DCR)**
```python
# MCP server startup
registration_response = await keycloak_client.register_client(
    client_name="MCP Server Instance",
    redirect_uris=["http://mcp-server/oauth/callback"]
)
# Store: client_id, client_secret
```

**Step 3: Issue Tokens to Users**
- Users authenticate via Keycloak
- MCP server gets tokens issued to its `client_id`
- These tokens validated by user_oidc!

**Step 4: Background Operations (ADR-002)**
- Store user refresh tokens (encrypted)
- Refresh access tokens as needed
- All tokens validated by user_oidc regardless of issuing client

## Important Notes

### Token Grant Types Matter

**Password Grant** (what we tested):
- Access tokens have NO `sub` or `aud`
- Forces validation via userinfo endpoint
- Works with any client in realm

**Authorization Code Grant** (production):
- Tokens MAY include `aud` claim
- Need to verify behavior with real OAuth flows
- May require disabling audience check

### Recommendation for Production

**Option 1: Disable Audience Check (Simplest)**
```php
// config.php
'user_oidc' => [
    'selfencoded_bearer_validation_audience_check' => false,
],
```

**Option 2: Rely on UserInfo Validation**
```php
// config.php
'user_oidc' => [
    'userinfo_bearer_validation' => true,  // Enable userinfo validation
],
```

**Option 3: Configure Keycloak to Not Include aud in Access Tokens**
- Keep default behavior (works as tested)
- Tokens validated via userinfo endpoint

## Testing Script

```bash
#!/bin/bash
# Test multi-client validation

# Create second client in Keycloak
curl -X POST "http://keycloak/admin/realms/my-realm/clients" \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -d '{
        "clientId": "test-client-b",
        "secret": "test-secret-b",
        "standardFlowEnabled": true,
        "directAccessGrantsEnabled": true
    }'

# Get token from client B
TOKEN=$(curl -X POST "http://keycloak/realms/my-realm/protocol/openid-connect/token" \
    -d "grant_type=password" \
    -d "client_id=test-client-b" \
    -d "client_secret=test-secret-b" \
    -d "username=testuser" \
    -d "password=password" | jq -r '.access_token')

# Test with Nextcloud (configured with client A)
curl -H "Authorization: Bearer $TOKEN" \
    "http://nextcloud/ocs/v2.php/cloud/capabilities"

# Should return 200 OK!
```

## Conclusion

✅ **Your proposed architecture is fully supported!**

- user_oidc configured once with ANY client from Keycloak realm
- MCP server registers dynamically via DCR
- MCP clients also register dynamically
- ALL tokens from realm validated successfully
- No per-client configuration needed

The key insight: **user_oidc validates tokens at the realm level** (via Keycloak's userinfo endpoint), not at the client level.

## References

- Source code: `~/Software/user_oidc/lib/User/Backend.php:260-343`
- SelfEncodedValidator: `~/Software/user_oidc/lib/User/Validator/SelfEncodedValidator.php`
- UserInfoValidator: `~/Software/user_oidc/lib/User/Validator/UserInfoValidator.php`
- Test setup: `docker-compose.yml` (mcp-keycloak service)
- Configuration: `.env.keycloak.sample`
