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

**Affected Component**: `user_oidc` app

**Issue**: Bearer token authentication fails for app-specific APIs (Notes, Calendar, etc.) with `401 Unauthorized` errors, even though OCS APIs work correctly.

**Root Cause**: The `CORSMiddleware` in Nextcloud logs out sessions created by Bearer token authentication when CSRF tokens are missing, which breaks API requests.

**Solution**: Set the `app_api` session flag during Bearer token authentication to bypass CSRF checks.

**Upstream PR**: [nextcloud/user_oidc#1221](https://github.com/nextcloud/user_oidc/issues/1221)

**Workaround**: Manually apply the patch to `lib/User/Backend.php` in the `user_oidc` app

**Impact**:
- ✅ **Works**: OCS APIs (`/ocs/v2.php/cloud/capabilities`)
- ❌ **Requires Patch**: App APIs (`/apps/notes/api/`, `/apps/calendar/`, etc.)

**Files Modified**: `lib/User/Backend.php` in `user_oidc` app

**Patch Summary**:
```php
// Add before successful Bearer token authentication returns
$this->session->set('app_api', true);
```

This is added at lines ~243, ~310, ~315, and ~337 in `Backend.php`.

---

### 2. PKCE Support Advertisement in Discovery

**Status**: 🟢 **PR Submitted** (Pending Review)

**Affected Component**: `oidc` app

**Issue**: The OIDC discovery endpoint (`/.well-known/openid-configuration`) does not advertise PKCE support in the `code_challenge_methods_supported` field.

**Why It Matters**:
- MCP specification requires PKCE with S256 code challenge method
- RFC 8414 states that absence of `code_challenge_methods_supported` means PKCE is **not supported**
- Some MCP clients may reject providers without proper PKCE advertisement

**Current Behavior**:
- PKCE **functionally works** (the OIDC app accepts and validates PKCE)
- PKCE just isn't **advertised** in discovery metadata

**Recommended Fix**: Update `oidc` app to include:
```json
{
  "code_challenge_methods_supported": ["S256"]
}
```

**Workaround**: The MCP server implements PKCE validation and logs a warning if not advertised. Functionality still works.

**Upstream PR**: [H2CK/oidc#584](https://github.com/H2CK/oidc/pull/584) - Submitted 2025-10-13
- **Changes**: Adds `code_challenge_methods_supported: ["S256"]` to discovery document when PKCE is enabled
- **Size**: +5 lines added, 0 deleted
- **Status**: Open, awaiting review

---

## Upstream PRs Status

| PR/Issue | Component | Status | Priority | Notes |
|----------|-----------|--------|----------|-------|
| [user_oidc#1221](https://github.com/nextcloud/user_oidc/issues/1221) | `user_oidc` | 🟡 Open | High | Required for app-specific APIs |
| [H2CK/oidc#584](https://github.com/H2CK/oidc/pull/584) | `oidc` | 🟢 PR Open | Medium | PKCE advertisement for standards compliance |

## What Works Without Patches

The following functionality works **out of the box** without any patches:

✅ **OAuth Flow**:
- OIDC discovery
- Dynamic client registration
- Authorization code flow with PKCE
- Token exchange
- Userinfo endpoint

✅ **MCP Server as Resource Server**:
- Token validation via userinfo
- Per-user client instances
- Token caching

✅ **Nextcloud OCS APIs**:
- Capabilities endpoint
- All OCS-based APIs

## What Requires Patches

The following functionality requires upstream patches:

🟡 **App-Specific APIs** (Requires user_oidc#1221):
- Notes API (`/apps/notes/api/`)
- Calendar API (CalDAV)
- Contacts API (CardDAV)
- Deck API
- Tables API
- Custom app APIs

🟡 **Standards Compliance** (PKCE advertisement):
- Full RFC 8414 compliance
- MCP client compatibility guarantee

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
uv run pytest tests/integration/test_oauth_playwright.py --browser firefox -v

# Tests verify:
# - OAuth flow completion
# - Token validation
# - MCP tool calls with Bearer tokens
# - Notes API access (requires patch)
```

## Monitoring Upstream Progress

To track progress on these issues:

1. **Watch the upstream repositories**:
   - [nextcloud/user_oidc](https://github.com/nextcloud/user_oidc)
   - [nextcloud/oidc](https://github.com/nextcloud/oidc)

2. **Subscribe to specific issues**:
   - [user_oidc#1221](https://github.com/nextcloud/user_oidc/issues/1221) - Bearer token support

3. **Check Nextcloud release notes** for mentions of:
   - Bearer token authentication improvements
   - OIDC/OAuth enhancements
   - AppAPI compatibility

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

**Last Updated**: 2025-10-14

**Next Review**: When PR #584 or issue #1221 has activity
