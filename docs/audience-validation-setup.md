# Audience Validation Setup

## Overview

This document explains the **separate clients architecture** for Keycloak → MCP Server → Nextcloud integration, following OAuth 2.0 best practices and RFC 8707 (Resource Indicators).

## Architecture: Separate Clients Pattern

```
Keycloak Realm: nextcloud-mcp
├── Client: "nextcloud" (Resource Server)
│   └── Represents Nextcloud as a protected resource
│   └── Used by user_oidc for bearer token validation
│   └── Validates tokens with aud="nextcloud"
│
└── Client: "nextcloud-mcp-server" (OAuth Client)
    └── MCP Server uses this to REQUEST tokens
    └── Issues tokens with aud="nextcloud" (targeting resource)
    └── Future: aud=["nextcloud", "other-service"]

Token Flow:
MCP Server (client: nextcloud-mcp-server)
  ↓ requests token from Keycloak
Token issued:
  - aud: "nextcloud"             (intended for Nextcloud resource)
  - azp: "nextcloud-mcp-server"  (requested by MCP Server)
  - preferred_username: "admin"  (on behalf of user)
  ↓ sent to Nextcloud API
Nextcloud user_oidc (client: nextcloud)
  ✓ validates aud matches configured client_id
```

**Key Benefits**:
- ✅ **Proper OAuth separation**: OAuth client ≠ resource server
- ✅ **Future extensibility**: MCP Server can request multi-resource tokens
- ✅ **RFC 8707 compliance**: Audience indicates intended resource
- ✅ **Clear requester identification**: azp claim identifies MCP Server

## Token Claims

Tokens issued by the `nextcloud-mcp-server` client contain:

- **`aud: "nextcloud"`** - Audience: Token intended for Nextcloud resource server (matches user_oidc client_id)
- **`azp: "nextcloud-mcp-server"`** - Authorized Party: Identifies MCP Server as the OAuth client that requested the token
- **`preferred_username: "admin"`** - User identifier (Keycloak uses this for password grant; `sub` for authorization_code grant)
- **`scope: "openid profile email offline_access"`** - Requested scopes including offline access for background jobs

**How user_oidc Validates**:
1. SelfEncodedValidator checks: `aud == user_oidc.client_id`?
   - ✓ "nextcloud" == "nextcloud" → PASS
2. Fast JWT verification with JWKS (no HTTP call to userinfo endpoint)
3. User provisioned based on `preferred_username` or `sub` claim

**For Background Jobs**:
- MCP Server stores encrypted refresh tokens
- Refreshes access tokens when needed
- All tokens have `aud: "nextcloud"` → validated by user_oidc
- No admin credentials required

## Configuration

The configuration requires **two separate clients** in Keycloak:

1. **`nextcloud`** - Resource server client (for user_oidc validation)
2. **`nextcloud-mcp-server`** - OAuth client (for MCP Server to request tokens)

### 1. Keycloak - Create Resource Server Client

First, create the `nextcloud` client that represents Nextcloud as a resource server:

**Via Keycloak Admin API:**

```bash
# Get admin token
ADMIN_TOKEN=$(curl -X POST "http://localhost:8888/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=admin-cli" \
  -d "username=admin" \
  -d "password=admin" | jq -r '.access_token')

# Create 'nextcloud' resource server client
curl -X POST "http://localhost:8888/admin/realms/nextcloud-mcp/clients" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "clientId": "nextcloud",
    "name": "Nextcloud Resource Server",
    "description": "Resource server for Nextcloud APIs - used by user_oidc for bearer token validation",
    "enabled": true,
    "clientAuthenticatorType": "client-secret",
    "secret": "nextcloud-secret-change-in-production",
    "bearerOnly": true,
    "standardFlowEnabled": false,
    "directAccessGrantsEnabled": false,
    "serviceAccountsEnabled": false,
    "publicClient": false
  }'
```

**Via Realm Export** (`keycloak/realm-export.json`):

```json
{
  "clients": [
    {
      "clientId": "nextcloud",
      "name": "Nextcloud Resource Server",
      "enabled": true,
      "bearerOnly": true,
      "secret": "nextcloud-secret-change-in-production"
    }
  ]
}
```

### 2. Keycloak - Create OAuth Client with Audience Mapper

Next, create the `nextcloud-mcp-server` client that MCP Server uses to request tokens:

**Via Keycloak Admin API:**

```bash
# Create 'nextcloud-mcp-server' OAuth client
curl -X POST "http://localhost:8888/admin/realms/nextcloud-mcp/clients" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "clientId": "nextcloud-mcp-server",
    "name": "Nextcloud MCP Server",
    "enabled": true,
    "clientAuthenticatorType": "client-secret",
    "secret": "mcp-secret-change-in-production",
    "standardFlowEnabled": true,
    "directAccessGrantsEnabled": true,
    "redirectUris": ["http://localhost:*/callback"]
  }'

# Get client internal ID
CLIENT_ID=$(curl "http://localhost:8888/admin/realms/nextcloud-mcp/clients" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.[] | select(.clientId=="nextcloud-mcp-server") | .id')

# Add audience mapper targeting 'nextcloud' resource
curl -X POST "http://localhost:8888/admin/realms/nextcloud-mcp/clients/$CLIENT_ID/protocol-mappers/models" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "audience-nextcloud",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "consentRequired": false,
    "config": {
      "included.custom.audience": "nextcloud",
      "access.token.claim": "true",
      "id.token.claim": "false"
    }
  }'
```

**Option B: Via Realm Export** (for infrastructure-as-code)

Update `keycloak/realm-export.json`:

```json
{
  "clients": [
    {
      "clientId": "nextcloud-mcp-server",
      "name": "Nextcloud MCP Server",
      "protocolMappers": [
        {
          "name": "audience-nextcloud-mcp-server",
          "protocol": "openid-connect",
          "protocolMapper": "oidc-audience-mapper",
          "consentRequired": false,
          "config": {
            "included.custom.audience": "nextcloud-mcp-server",
            "access.token.claim": "true",
            "id.token.claim": "false"
          }
        }
      ]
    }
  ]
}
```

Then re-import realm or restart Keycloak.

**Option C: Via Keycloak Admin UI**

1. Go to Keycloak Admin Console → Realm → Clients → `nextcloud-mcp-server`
2. Click "Client scopes" tab
3. Click "Add client scope" → "Create dedicated scope"
4. Add protocol mapper: "Audience"
   - Mapper Type: `Audience`
   - Included Custom Audience: `nextcloud`
   - Add to access token: ON
   - Add to ID token: OFF

### 3. Nextcloud user_oidc - Configure Resource Server Client

Configure user_oidc to use the `nextcloud` resource server client:

```bash
docker compose exec app php occ user_oidc:provider keycloak \
  --clientid="nextcloud" \
  --clientsecret="nextcloud-secret-change-in-production" \
  --discoveryuri="http://keycloak:8080/realms/nextcloud-mcp/.well-known/openid-configuration" \
  --check-bearer=1 \
  --bearer-provisioning=1 \
  --unique-uid=1 \
  --mapping-uid="sub" \
  --mapping-display-name="name" \
  --mapping-email="email"
```

**Result**: user_oidc validates tokens with `aud="nextcloud"` using SelfEncodedValidator (fast JWT verification).

### 3. Nextcloud user_oidc - Realm-Level Validation

Nextcloud's `user_oidc` app validates at **realm level** via userinfo endpoint:

- ✅ **No configuration needed** - works automatically
- ✅ Validates any token from Keycloak realm
- ✅ Audience check is **optional** (disabled by default)

**Optional: Disable strict audience checking** (if enabled):

```bash
docker compose exec app php occ config:app:set user_oidc \
  selfencoded_bearer_validation_audience_check --value=false --type=boolean
```

## Verification

### 1. Check Token Claims

```bash
# Get token from Keycloak
TOKEN=$(curl -X POST "http://localhost:8888/realms/nextcloud-mcp/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=nextcloud-mcp-server" \
  -d "client_secret=mcp-secret-change-in-production" \
  -d "username=admin" \
  -d "password=admin" | jq -r '.access_token')

# Decode JWT
echo $TOKEN | cut -d'.' -f2 | base64 -d | jq '.'

# Should show:
{
  "aud": "nextcloud",  # ✓ Intended for Nextcloud
  "azp": "nextcloud-mcp-server",  # ✓ Requested by MCP Server
  "iss": "http://localhost:8888/realms/nextcloud-mcp",
  "scope": "openid email profile offline_access",
  ...
}
```

### 2. Test with Nextcloud API

```bash
# Token should be accepted
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8080/ocs/v2.php/cloud/capabilities"

# Should return HTTP 200 OK
```

### 3. Test Audience Rejection

```bash
# Get token from different client (without audience mappers)
TOKEN_WRONG=$(curl -X POST "http://localhost:8888/realms/nextcloud-mcp/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=test-client-b" \
  -d "client_secret=test-secret-b" \
  -d "username=admin" \
  -d "password=admin" | jq -r '.access_token')

# This token has NO audience claim - should be rejected by MCP server
# (But accepted by Nextcloud user_oidc which validates at realm level)
```

## Token Flow Example

### Successful Request (Background Job)

```
1. User authorizes MCP Client via OAuth
   └─ MCP Server gets refresh token (stored encrypted)

2. Background worker needs to sync data
   └─ MCP Server refreshes access token from Keycloak
   └─ Token issued with aud: "nextcloud", azp: "nextcloud-mcp-server"

3. MCP Server → Nextcloud API (with token)
   └─ user_oidc validates via userinfo endpoint ✓
   └─ Nextcloud identifies:
       - Token intended for Nextcloud (aud: "nextcloud")
       - Request from MCP Server (azp: "nextcloud-mcp-server")
       - On behalf of user (sub: "user-id")

4. Success! MCP Server can act on behalf of user in background.
```

### Rejected Request

```
1. Attacker gets token for different client
   └─ Token has aud: "other-service"

2. Attacker → Nextcloud API (with wrong token)
   └─ user_oidc validates via userinfo endpoint
   └─ Token validation fails (invalid/expired/wrong realm)
   └─ HTTP 401 Unauthorized

3. Request blocked - token not valid for this realm/service
```

## OAuth Flows and User Consent

### When Does the User Grant Consent?

User consent happens during the **Authorization Code Flow** (production OAuth):

```
1. User clicks "Connect" in MCP Client (e.g., Claude Desktop)
2. MCP Client initiates OAuth flow by opening browser to Keycloak:
   https://keycloak/realms/nextcloud-mcp/protocol/openid-connect/auth?
     client_id=nextcloud-mcp-server&
     redirect_uri=<mcp-client-redirect-uri>&
     response_type=code&
     scope=openid profile email offline_access

3. Keycloak shows login screen (if not logged in)
4. **Keycloak shows consent screen:**
   "Nextcloud MCP Server wants to access your Nextcloud data on your behalf"
   Requested permissions:
   - Access your profile (openid, profile, email)
   - Offline access (background operations with refresh tokens)

5. User clicks "Allow" → grants consent
6. Keycloak redirects back to MCP Client with authorization code
7. MCP Client exchanges code for tokens (receives access + refresh tokens)
8. MCP Client shares tokens with MCP Server via MCP protocol
9. MCP Server stores refresh token encrypted for background operations
```

**Key Architecture Notes:**
- **MCP Server is a protected resource** (requires OAuth to access)
- **MCP Client** (Claude Desktop) is the OAuth client that initiates the flow
- **MCP Client handles the redirect** and token exchange with Keycloak
- **MCP Client shares refresh token** with MCP Server so it can act on behalf of user in background

**Key Points:**
- ✅ **Explicit user consent** before any access
- ✅ **Scopes displayed** so user knows what's being requested
- ✅ **Offline access** must be explicitly granted (for background jobs)
- ✅ **Revocable** - user can revoke consent in Keycloak at any time

### Grant Types

Our architecture supports multiple OAuth grant types:

**1. Authorization Code + PKCE (Production)**
```
Use case: Interactive login from MCP clients
Consent: Yes - explicit user authorization
Tokens: Access token + Refresh token (if offline_access granted)
Security: PKCE prevents authorization code interception
```

**2. Password Grant (Testing Only)**
```
Use case: Integration testing with docker-compose
Consent: No - username/password provided directly
Tokens: Access token + Refresh token
Security: NOT for production - exposes user credentials
```

**3. Refresh Token Grant (Background Jobs)**
```
Use case: MCP Server refreshing expired access tokens
Consent: No new consent - uses previously granted refresh token
Tokens: New access token (refresh token may rotate)
Security: Refresh tokens stored encrypted, rotated on use
```

## Authentication Strategies for Background Jobs

### Current Approach: Offline Access with Refresh Tokens (Tier 1)

The MCP server currently uses **offline_access** scope to enable background operations:

**How it works:**
1. User grants `offline_access` scope during OAuth consent
2. MCP Client receives refresh token from Keycloak
3. MCP Client shares refresh token with MCP Server via MCP protocol
4. MCP Server stores refresh token encrypted (see ADR-002)
5. Background jobs exchange refresh token for fresh access tokens as needed

**Benefits:**
- ✅ Works today with Keycloak and all OIDC providers
- ✅ Standard OAuth pattern (RFC 6749)
- ✅ Explicit user consent to `offline_access` scope
- ✅ MCP Server can act on behalf of user in background

**Limitations:**
- ⚠️ Requires secure token storage on MCP Server
- ⚠️ MCP Client must trust MCP Server with refresh token
- ⚠️ Weak audit trail - API requests appear to come from user directly
- ⚠️ No visibility that MCP Server is the actual actor

### Future Enhancement: Token Exchange with Delegation (Tier 2)

**RFC 8693 Delegation** would provide better audit trail and security:

**How it would work:**
1. User grants `may_act:nextcloud-mcp-server` scope during authentication
2. Subject token includes: `{ "may_act": { "client": "nextcloud-mcp-server" } }`
3. MCP Server has its own service account token (actor_token)
4. Background job requests token exchange:
   - `subject_token` (user's token with may_act claim)
   - `actor_token` (mcp-server's service token)
5. Keycloak validates actor matches may_act claim
6. Returns delegated token: `{ "sub": "user", "act": "nextcloud-mcp-server" }`

**Benefits:**
- ✅ Better audit trail - Nextcloud APIs see both user and actor
- ✅ No token storage needed (tokens generated on-demand)
- ✅ Fine-grained permissions via `may_act` claim
- ✅ User explicitly consents to MCP Server acting on their behalf
- ✅ RFC 8693 compliant

**Current Status:**
- ❌ **NOT implemented in Keycloak yet** ([Issue #38279](https://github.com/keycloak/keycloak/issues/38279))
- ❌ Would require custom implementation or waiting for upstream
- 📝 Proposal includes `act` claim and `may_act` consent mechanism

**Why Not Available:**
- Keycloak supports **impersonation** (changes `sub` claim), but not **delegation** (`act` claim)
- Impersonation has poor audit trail (actor invisible)
- Delegation proposal is open but not implemented yet

**Reference:** See `docs/ADR-002-vector-sync-authentication.md` for detailed comparison of authentication tiers.

## Security Benefits

1. **Intent Validation**: Tokens explicitly declare Nextcloud as the intended recipient via `aud` claim
2. **Requester Identification**: The `azp` claim identifies MCP Server as the requester
3. **User Context**: The `sub` claim preserves user identity for audit and authorization
4. **Background Jobs**: Refresh tokens enable MCP Server to act on behalf of users without admin credentials
5. **OAuth Standards**: Follows RFC 8707 (Resource Indicators) and RFC 6749 (OAuth 2.0)

**Current Limitations:**
- API requests from background jobs appear to come from user directly (no `act` claim yet)
- See "Authentication Strategies for Background Jobs" section for future delegation support

## Token Claims

### Key Claims

- **`aud: "nextcloud"`** - Audience: Token intended for Nextcloud APIs
- **`azp: "nextcloud-mcp-server"`** - Authorized Party: MCP Server requested the token
- **`sub: "user-id"`** - Subject: User on whose behalf the request is made
- **`scope: "openid profile email offline_access"`** - Requested scopes including offline access for background jobs

### Client Naming

The Keycloak client is named `nextcloud-mcp-server` to clarify:
- **MCP Server** uses this client to get tokens for Nextcloud
- **MCP Clients** (like Claude Desktop) connect to MCP Server via separate OAuth flows
- **Not** named "mcp-client" to avoid confusion about which component is the client

## Troubleshooting

### Token Has No Audience

**Symptom**: `"aud": null` in decoded JWT

**Cause**: Protocol mappers not configured

**Solution**: Add audience mappers via Keycloak Admin API (see Configuration section)

### MCP Server Rejects Token

**Symptom**: HTTP 401 with "JWT validation failed"

**Cause**: Token audience doesn't match expected value

**Solution**:
1. Check token has correct `aud` claim
2. Verify MCP server expects correct audience value in code
3. Check logs for specific JWT validation error

### Nextcloud Rejects Token

**Symptom**: HTTP 401 from Nextcloud API

**Cause**: User not provisioned or token invalid

**Solution**:
1. Check user_oidc provider is configured: `php occ user_oidc:provider keycloak`
2. Check bearer validation enabled: `--check-bearer=1`
3. Test token with userinfo endpoint: `curl -H "Authorization: Bearer $TOKEN" http://keycloak/realms/.../userinfo`

## Related Documentation

- **Multi-client validation**: `docs/keycloak-multi-client-validation.md`
- **ADR-002**: `docs/ADR-002-vector-sync-authentication.md`
- **OAuth setup**: `docs/oauth-setup.md`
- **Keycloak integration**: `docs/keycloak-integration.md` (if created)

## References

- [RFC 8707 - Resource Indicators for OAuth 2.0](https://datatracker.ietf.org/doc/html/rfc8707)
- [OIDC Core - ID Token aud claim](https://openid.net/specs/openid-connect-core-1_0.html#IDToken)
- [Keycloak Audience Protocol Mappers](https://www.keycloak.org/docs/latest/server_admin/#_audience)
