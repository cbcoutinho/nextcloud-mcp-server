# OAuth Upstream Status

This document tracks the status of upstream patches and pull requests required for full OAuth functionality.

## Overview

The Nextcloud MCP Server's OAuth implementation relies on two Nextcloud apps:
- **`oidc`** - OIDC Identity Provider (Authorization Server)
- **`user_oidc`** - OpenID Connect user backend (Token validation)

While the core OAuth flow works, there are **pending upstream improvements** that enhance functionality and standards compliance.

## Required Patches

### 1. Bearer Token Support for Non-OCS Endpoints

**Status**: 🟡 **Patch Required** (Pending Upstream)

**Affected Component**: **Nextcloud core server** (`CORSMiddleware`)

**Issue**: Bearer token authentication fails for app-specific APIs (Notes, Calendar, etc.) with `401 Unauthorized` errors, even though OCS APIs work correctly.

**Root Cause**: The `CORSMiddleware` in Nextcloud core server logs out sessions when CSRF tokens are missing. Bearer token authentication creates a session (via `user_oidc` app), but doesn't include CSRF tokens (stateless authentication). The middleware detects the logged-in session without CSRF token and calls `session->logout()`, invalidating the request.

**Solution**: Allow Bearer token requests to bypass CORS/CSRF checks in `CORSMiddleware`, since Bearer tokens are stateless and don't require CSRF protection.

**Upstream PR**: [nextcloud/server#55878](https://github.com/nextcloud/server/pull/55878)

**Workaround**: Manually apply the patch to `lib/private/AppFramework/Middleware/Security/CORSMiddleware.php` in Nextcloud core server

**Impact**:
- ✅ **Works**: OCS APIs (`/ocs/v2.php/cloud/capabilities`)
- ❌ **Requires Patch**: App APIs (`/apps/notes/api/`, `/apps/calendar/`, etc.)

**Files Modified**: `lib/private/AppFramework/Middleware/Security/CORSMiddleware.php` in **Nextcloud core server**

**Patch Summary**:
```php
// Allow Bearer token authentication for CORS requests
// Bearer tokens are stateless and don't require CSRF protection
$authorizationHeader = $this->request->getHeader('Authorization');
if (!empty($authorizationHeader) && str_starts_with($authorizationHeader, 'Bearer ')) {
    return;
}
```

This is added before the CSRF check at line ~73 in `CORSMiddleware.php`.

---

### 2. JWT Token Support, Introspection, and Scope Validation

**Status**: ✅ **Complete** (Merged Upstream)

**Affected Component**: `oidc` app

**Issue**: The OIDC app needed support for JWT tokens, token introspection, and enhanced scope validation for fine-grained authorization.

**Resolution**: Complete JWT and scope validation support has been implemented and merged:

**Upstream PR**: [H2CK/oidc#585](https://github.com/H2CK/oidc/pull/585) - ✅ **Merged**
- **Changes**:
  - JWT token generation and validation
  - Token introspection endpoint (RFC 7662)
  - Enhanced scope validation and parsing
  - Custom scope support for Nextcloud apps
- **Status**: Merged and available in v1.10.0+ of the `oidc` app

---

### 3. User Consent Management

**Status**: ✅ **Complete** (Merged Upstream)

**Affected Component**: `oidc` app

**Issue**: The OIDC app needed proper user consent management for OAuth authorization flows.

**Resolution**: Complete user consent management has been implemented and merged:

**Upstream PR**: [H2CK/oidc#586](https://github.com/H2CK/oidc/pull/586) - ✅ **Merged**
- **Changes**:
  - User consent UI for OAuth authorization
  - Consent expiration and cleanup
  - Admin control for user consent settings
  - Consent tracking and management
- **Status**: Merged and available in v1.11.0+ of the `oidc` app

---

### 4. PKCE Support (RFC 7636)

**Status**: ✅ **Complete** (Merged Upstream)

**Affected Component**: `oidc` app

**Issue**: The OIDC app lacked PKCE (Proof Key for Code Exchange) implementation per RFC 7636.

**Resolution**: Full PKCE support has been implemented and merged upstream into the `oidc` app:

**Authorization Endpoint** (`/authorize`):
- Accepts `code_challenge` and `code_challenge_method` parameters
- Validates code_challenge format (43-128 characters, unreserved chars only)
- Supports both `S256` (SHA-256) and `plain` challenge methods
- Stores challenge and method in database for later verification

**Token Endpoint** (`/token`):
- Accepts `code_verifier` parameter
- Verifies code_verifier against stored code_challenge using proper algorithm
- Uses constant-time comparison to prevent timing attacks
- Enforces code_verifier requirement when PKCE was used in authorization

**Discovery Document**:
```json
{
  "code_challenge_methods_supported": ["S256", "plain"]
}
```

**Database**:
- New columns: `code_challenge` and `code_challenge_method` in `oc_oauth2_access_tokens`
- Migration included for existing installations

**Why It Mattered**:
- MCP specification requires PKCE with S256 code challenge method
- RFC 7636 PKCE provides security for public clients (no client secret)
- RFC 8414 states that absence of `code_challenge_methods_supported` means PKCE is **not supported**
- Prevents authorization code interception attacks

**Upstream PR**: [H2CK/oidc#584](https://github.com/H2CK/oidc/pull/584) - ✅ **Merged 2025-10-20**
- **Changes**: Complete PKCE implementation (+194 lines)
  - Authorization flow with code_challenge validation
  - Token exchange with code_verifier verification
  - Database schema updates
  - Discovery document updates
- **Status**: Merged and available in v1.10.0+ of the `oidc` app

---

## Upstream PRs Status

| PR/Issue | Component | Status | Priority | Notes |
|----------|-----------|--------|----------|-------|
| [server#55878](https://github.com/nextcloud/server/pull/55878) | Nextcloud core server | 🟡 Open | High | CORSMiddleware patch for Bearer tokens |
| [H2CK/oidc#586](https://github.com/H2CK/oidc/pull/586) | `oidc` | ✅ Merged | Medium | ✅ User consent complete (v1.11.0+) |
| [H2CK/oidc#585](https://github.com/H2CK/oidc/pull/585) | `oidc` | ✅ Merged | Medium | ✅ JWT tokens, introspection, scope validation (v1.10.0+) |
| [H2CK/oidc#584](https://github.com/H2CK/oidc/pull/584) | `oidc` | ✅ Merged | ~~High~~ | ✅ PKCE support (RFC 7636) (v1.10.0+) |

## What Works Without Patches

The following functionality works **out of the box** without any patches:

✅ **OAuth Flow** (requires `oidc` app v1.10.0+):
- OIDC discovery with full PKCE support (RFC 7636)
- Dynamic client registration
- Authorization code flow with PKCE (S256 and plain methods)
- Token exchange with code_verifier verification
- User consent management
- Userinfo endpoint

✅ **Token Features** (requires `oidc` app v1.10.0+):
- JWT token generation and validation
- Token introspection endpoint (RFC 7662)
- Enhanced scope validation and parsing
- Custom scope support for Nextcloud apps

✅ **MCP Server as Resource Server**:
- Token validation via userinfo
- Per-user client instances
- Token caching
- Scope-based authorization

✅ **Nextcloud OCS APIs**:
- Capabilities endpoint
- All OCS-based APIs

## What Requires Patches

The following functionality requires upstream patches:

🟡 **App-Specific APIs** (Requires Nextcloud core server CORSMiddleware patch):
- Notes API (`/apps/notes/api/`)
- Calendar API (CalDAV)
- Contacts API (CardDAV)
- Deck API
- Tables API
- Custom app APIs

✅ **Standards Compliance**: Now complete with `oidc` app v1.10.0+
- ✅ Full RFC 8414 compliance (PKCE advertisement)
- ✅ MCP client compatibility guarantee

## Installation Instructions

### For Development/Testing

If the upstream PRs are not yet merged, you can apply patches manually:

#### 1. Apply Bearer Token Patch

```bash
# SSH into Nextcloud server
cd /path/to/nextcloud/apps/user_oidc

# Download and apply patch
# (Patch file to be created once PR is ready)
wget https://github.com/nextcloud/user_oidc/pull/XXXX.patch
git apply XXXX.patch

# Or manually edit lib/User/Backend.php
# Add this line before each return statement in getCurrentUserId():
#   $this->session->set('app_api', true);
```

#### 2. Verify Installation

```bash
# Test with OAuth token
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://your.nextcloud.com/apps/notes/api/v1/notes

# Should return notes JSON (not 401)
```

### For Production

**Recommendation**: Wait for upstream PRs to be merged and included in official Nextcloud releases before deploying OAuth in production.

**Alternative**: Use a patched version of `user_oidc` app in your deployment:
1. Fork the `user_oidc` app
2. Apply the required patches
3. Install your patched version
4. Document the changes for your team

## Testing

The integration test suite validates OAuth functionality:

```bash
# Start OAuth-enabled MCP server
docker-compose up --build -d mcp-oauth

# Run comprehensive OAuth tests
uv run pytest tests/client/test_oauth_playwright.py --browser firefox -v

# Tests verify:
# - OAuth flow completion
# - Token validation
# - MCP tool calls with Bearer tokens
# - Notes API access (requires patch)
```

## Monitoring Upstream Progress

To track progress on remaining issues:

1. **Watch the upstream repository**:
   - [nextcloud/server](https://github.com/nextcloud/server)

2. **Subscribe to the CORSMiddleware PR**:
   - [server#55878](https://github.com/nextcloud/server/pull/55878) - CORSMiddleware Bearer token support

3. **Check Nextcloud server release notes** for mentions of:
   - Bearer token authentication improvements
   - CORS middleware enhancements
   - OAuth/OIDC API compatibility

4. **Completed upstream work** (no monitoring needed):
   - ✅ [H2CK/oidc#584](https://github.com/H2CK/oidc/pull/584) - PKCE support (v1.10.0+)
   - ✅ [H2CK/oidc#585](https://github.com/H2CK/oidc/pull/585) - JWT, introspection, scopes (v1.10.0+)
   - ✅ [H2CK/oidc#586](https://github.com/H2CK/oidc/pull/586) - User consent (v1.11.0+)

## Contributing

Want to help get these patches merged?

1. **Test the patches**: Run the integration tests and report results
2. **Review PRs**: Provide feedback on upstream pull requests
3. **Document issues**: Report any problems or edge cases
4. **Contribute code**: Submit improvements or fixes to upstream

## Timeline Expectations

**Best Case**: PRs merged in next Nextcloud minor release (est. 3-6 months)

**Realistic**: PRs reviewed and merged within 6-12 months

**Meanwhile**: Use the workarounds documented in this guide

## See Also

- [OAuth Architecture](oauth-architecture.md) - How OAuth works in this implementation
- [OAuth Troubleshooting](oauth-troubleshooting.md) - Common issues and solutions
- [OAuth Setup Guide](oauth-setup.md) - Configuration instructions

---

**Last Updated**: 2025-11-02

**Next Review**: When Nextcloud server CORSMiddleware PR has activity
