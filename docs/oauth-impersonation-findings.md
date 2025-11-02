# OAuth Impersonation Investigation Findings

**Date**: 2025-11-02
**Last Updated**: 2025-11-02 (Token Exchange Resolution)
**Status**: Implementation Complete - Token Exchange Working
**Conclusion**: Keycloak Standard Token Exchange (RFC 8693) working for internal-to-internal token exchange. User impersonation requires Legacy V1.

---

## ⚠️ IMPORTANT UPDATE (2025-11-02)

**This document contains outdated information regarding service account tokens.**

After implementation and testing, we discovered that service account tokens (`client_credentials` grant) **violate OAuth "act on-behalf-of" principles** by creating Nextcloud user accounts (e.g., `service-account-nextcloud-mcp-server`). This approach has been **REJECTED** and moved to ADR-002's "Will Not Implement" section.

**Key Changes:**
- ❌ **Service account tokens (client_credentials) are INVALID** - Creates user accounts, breaks audit trail
- ✅ **Token exchange (RFC 8693) is the correct approach** - Implemented and working (ADR-002 Tier 2)
- ✅ **Offline access with refresh tokens** - Still valid for background operations (ADR-002 primary approach)

**For current architecture, see**: `docs/ADR-002-vector-sync-authentication.md`

---

## Summary

We investigated options for implementing user impersonation to enable background operations without requiring admin credentials (ADR-002 Tier 2). Here are the findings:

## 1. Keycloak Token Exchange (RFC 8693)

### What We Implemented
- ✅ Service account token acquisition (`client_credentials` grant)
- ✅ `get_service_account_token()` method in `KeycloakOAuthClient`
- ✅ `exchange_token_for_user()` method implementing RFC 8693
- ✅ Token exchange configuration in Keycloak realm

### What Works ✅
**Keycloak Standard V2 Token Exchange (RFC 8693) is WORKING**:
- ✅ Service account token acquisition via `client_credentials` grant
- ✅ Token exchange for internal-to-internal tokens
- ✅ Audience and scope modifications
- ✅ Integration with Nextcloud APIs using exchanged tokens

**Configuration Requirements**:
To enable Standard Token Exchange in Keycloak 26.2+, add to client attributes in `realm-export.json`:
```json
"attributes": {
  "token.exchange.grant.enabled": "true",
  "client.token.exchange.standard.enabled": "true"
}
```

### Limitations
Keycloak Standard V2 does NOT support:
- ❌ User impersonation (`requested_subject` parameter)
- ❌ Cross-client delegation (limited to same realm)

These features require Legacy V1 with `--features=preview`

### Alternative: Keycloak Legacy V1
Keycloak Legacy Token Exchange (V1) WOULD support user impersonation, but:
- ❌ Requires `--features=preview --features=token-exchange` flag
- ❌ Not suitable for production
- ❌ Deprecated and being phased out

**Decision**: Not viable for production use.

---

## 2. Nextcloud OIDC App Token Exchange

### Discovery Endpoint Analysis
```json
{
  "grant_types_supported": [
    "authorization_code",
    "implicit"
  ]
}
```

### Findings
❌ **Nextcloud OIDC app does NOT support**:
- RFC 8693 token exchange
- `client_credentials` grant
- `refresh_token` grant (refresh tokens not issued)
- User impersonation APIs

The Nextcloud OIDC app is a basic OAuth 2.0 provider focused on:
- Authorization code flow for user login
- JWT tokens for API access
- Scope-based authorization

It is NOT designed for:
- Service accounts
- Token delegation
- Background operations

**Decision**: Not viable - missing required grant types.

---

## 3. Nextcloud Impersonate App

### What It Provides
✅ Admin users can impersonate other users via:
- UI: Settings → Users → Impersonate button
- API: `POST /apps/impersonate/user` with `userId` parameter

### How It Works
```php
// From SettingsController.php
public function impersonate(string $userId): JSONResponse {
    // 1. Verify admin/delegated admin permissions
    // 2. Check target user has logged in before
    // 3. Set session: $this->userSession->setUser($impersonatee)
    // 4. Return success
}
```

### Requirements
- ✅ Admin credentials
- ✅ Session-based authentication (cookies)
- ✅ CSRF token
- ✅ Target user must have logged in at least once
- ❌ Not compatible with encryption-enabled instances

### Limitations for Background Workers
❌ **Session-based, not stateless**:
- Requires maintaining HTTP session/cookies
- Not suitable for distributed workers
- Can't use with bearer tokens
- Requires re-authentication periodically

❌ **Security concerns**:
- Requires admin credentials stored on server
- All impersonated actions logged as target user
- Violates principle of least privilege

**Decision**: Not suitable for background operations - session-based architecture incompatible with stateless OAuth/bearer token model.

---

## 4. What Actually Works

### Option A: Admin Credentials (Current Implementation)
✅ **BasicAuth mode with admin account**:
```python
client = NextcloudClient.from_env()  # Uses NEXTCLOUD_USERNAME/PASSWORD
# Can access all APIs with admin permissions
```

**Pros**:
- Simple, works immediately
- Full access to all APIs

**Cons**:
- Requires admin credentials stored on server
- No per-user permission scoping
- Security risk if credentials leaked
- Violates ADR-002 goals

**Status**: Available but not recommended for production.

### Option B: Service Account with Scoped Permissions
✅ **Create dedicated service account**:
1. Create `mcp-sync` user in Nextcloud
2. Grant specific permissions (group memberships, shares)
3. Use those credentials for background operations

**Pros**:
- Dedicated account, easier to audit
- Can limit permissions via Nextcloud groups
- Works with current BasicAuth implementation

**Cons**:
- Still requires credentials storage
- Can't truly act "as" individual users
- Limited by Nextcloud's permission model

**Status**: Best available option without OAuth delegation.

---

## 5. Recommendations

### Short Term (Immediate)
**Use Service Account Pattern**:
```python
# Background worker configuration
SYNC_ACCOUNT_USERNAME=mcp-sync
SYNC_ACCOUNT_PASSWORD=<secure-password>

# Create service account with limited permissions
docker compose exec app php occ user:add mcp-sync
docker compose exec app php occ group:adduser <appropriate-group> mcp-sync
```

**Benefits**:
- Works with existing implementation
- Better than admin credentials
- Auditable

### Medium Term (If OAuth Delegation Required)
**Wait for proper standards support**:
- Monitor Keycloak for Standard V2 improvements
- Contribute to/request Nextcloud OIDC app enhancements
- Consider alternative identity providers (e.g., Authelia, Authentik)

### Long Term (Ideal Solution)
**Implement proper OAuth delegation**:
1. Use identity provider that supports RFC 8693 properly (e.g., Auth0, Okta)
2. Or implement custom delegation endpoint in Nextcloud
3. Or propose MCP protocol extension for refresh token sharing

---

## 6. Updated ADR-002 Status

| Tier | Solution | Status | Viability |
|------|----------|--------|-----------|
| **Tier 0** | Admin BasicAuth | ✅ Implemented | ⚠️ Works but not recommended |
| **Tier 1** | Offline Access (Refresh Tokens) | ⚠️ Infrastructure ready | ❌ MCP protocol limitation |
| **Tier 2** | Token Exchange (RFC 8693) | ✅ **WORKING** | ✅ **Internal token exchange functional** |
| **Tier 3** | Service Account (NEW) | ✅ Available | ✅ **RECOMMENDED for background ops** |

---

## 7. Implementation Status

### What Was Built
1. ✅ `RefreshTokenStorage` - SQLite + encryption (ready for future use)
2. ✅ `KeycloakOAuthClient.get_service_account_token()` - Works
3. ✅ `KeycloakOAuthClient.exchange_token_for_user()` - Implemented but non-functional
4. ✅ Token exchange configuration - Keycloak realm updated
5. ✅ Test scripts - Comprehensive testing completed

### What to Use
**For Background Operations**:
```python
# Use service account with BasicAuth
from nextcloud_mcp_server.client import NextcloudClient

# In background worker
sync_client = NextcloudClient(
    base_url=os.getenv("NEXTCLOUD_HOST"),
    username=os.getenv("SYNC_ACCOUNT_USERNAME"),
    password=os.getenv("SYNC_ACCOUNT_PASSWORD"),
)

# Perform operations
notes = await sync_client.notes.search_notes("important")
# Index to vector database, etc.
```

**For User Requests**:
```python
# Continue using OAuth bearer tokens
# Per-request client creation as currently implemented
client = get_client_from_context(ctx, nextcloud_host)
```

---

## 8. Files Modified/Created

### Implementation
- `nextcloud_mcp_server/auth/keycloak_oauth.py` - Token exchange methods
- `nextcloud_mcp_server/auth/refresh_token_storage.py` - Token storage (ready for future)
- `nextcloud_mcp_server/app.py` - OAuth configuration updates
- `keycloak/realm-export.json` - Token exchange enabled
- `pyproject.toml` - Added aiosqlite dependency

### Documentation
- `docs/oauth-impersonation-findings.md` - This document
- `docs/ADR-002-vector-sync-authentication.md` - Original architecture decision

### Tests
- `tests/manual/test_token_exchange.py` - Keycloak RFC 8693 testing
- `tests/manual/test_nextcloud_impersonate.py` - Nextcloud impersonate API testing

---

## 9. Conclusion

**Neither Keycloak nor Nextcloud currently provide viable OAuth-based user impersonation for background operations.**

The infrastructure is ready (token storage, exchange methods), but provider limitations prevent use.

**Recommended approach**: Use dedicated service account with appropriate Nextcloud permissions for background operations until proper OAuth delegation becomes available.

The implemented code remains valuable:
- Ready for future when providers add support
- Demonstrates proper OAuth patterns
- Test infrastructure for validation

---

## Appendix: Technical Details

### Keycloak Configuration Applied
```json
{
  "clientId": "nextcloud-mcp-server",
  "serviceAccountsEnabled": true,
  "attributes": {
    "token.exchange.grant.enabled": "true"
  }
}
```

### Test Results - UPDATED (2025-11-02)
```
✅ Service account token acquisition: WORKS
✅ Token exchange discovery: SUPPORTED
✅ Token exchange configuration: ENABLED
✅ Actual token exchange: WORKS (after adding client.token.exchange.standard.enabled)
✅ Nextcloud API access: WORKS with exchanged tokens
```

**Resolution**: The realm-export.json was missing the `client.token.exchange.standard.enabled` attribute. After adding this attribute to keycloak/realm-export.json:128, token exchange works correctly on fresh Keycloak imports.

### Nextcloud Impersonate Results
```
✓ App installation: SUCCESS
✓ Admin can impersonate: YES (session-based)
✗ Bearer token impersonate: NO (requires session cookies)
✗ Stateless impersonate: NOT AVAILABLE
```

---

## 10. Token Exchange Resolution (2025-11-02)

### Problem
Initial token exchange implementation was failing with:
```
"Standard token exchange is not enabled for the requested client"
```

### Root Cause
The `realm-export.json` was missing a critical attribute for Keycloak 26.2+ Standard Token Exchange:
- Had: `"token.exchange.grant.enabled": "true"` ✓
- Missing: `"client.token.exchange.standard.enabled": "true"` ❌

### Fix Applied
Updated `keycloak/realm-export.json` at line 128 to include both attributes:
```json
"attributes": {
  "pkce.code.challenge.method": "S256",
  "use.refresh.tokens": "true",
  "backchannel.logout.session.required": "true",
  "backchannel.logout.url": "http://app:80/index.php/apps/user_oidc/backchannel-logout/keycloak",
  "oauth2.device.authorization.grant.enabled": "false",
  "oidc.ciba.grant.enabled": "false",
  "client_credentials.use_refresh_token": "false",
  "display.on.consent.screen": "false",
  "token.exchange.grant.enabled": "true",
  "client.token.exchange.standard.enabled": "true"  // ADDED
}
```

### Verification
After recreating Keycloak with fresh realm import:
```bash
$ docker compose down -v keycloak && docker compose up -d keycloak
$ uv run python tests/manual/test_token_exchange.py
✅ Token Exchange Test PASSED
```

### Current Status
- ✅ RFC 8693 Token Exchange fully functional
- ✅ Service account token acquisition works
- ✅ Token exchange for internal tokens works
- ✅ Exchanged tokens validate with Nextcloud APIs
- ✅ Realm import automatically applies correct configuration
- ⚠️ User impersonation still requires Keycloak Legacy V1

### Files Modified
- `keycloak/realm-export.json` - Added `client.token.exchange.standard.enabled` attribute
- `docs/oauth-impersonation-findings.md` - Updated with resolution

### Testing
Run the complete token exchange flow:
```bash
uv run python tests/manual/test_token_exchange.py
```
