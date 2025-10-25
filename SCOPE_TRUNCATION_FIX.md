# JWT Scope Truncation Fix - Summary

## Problem
When using JWT tokens with many scopes, the `scope` claim in the JWT payload was being truncated, causing only 32 out of 90 tools to be visible to the MCP client.

## Root Cause
Multiple hardcoded string length limits in the Nextcloud OIDC app code:

1. **Database schema**: `oc_oidc_access_tokens.scope` column was `VARCHAR(128)` - too small for 247-character scope string
2. **Code truncation in TokenGenerationRequestListener.php**: `substr($scopes, 0, 128)` on line 83
3. **Code truncation in LoginRedirectorController.php**: `substr($scope, 0, 128)` on line 437
4. **Client scope limits**: Multiple places truncating `allowed_scopes` to 255 characters

## Solution
Fixed all truncation points to support up to 512 characters:

### Database Migration (Version0015Date20251123100100.php)
```php
// Increase oidc_clients.allowed_scopes from 256 to 512
$table->changeColumn('allowed_scopes', [
    'notnull' => false,
    'length' => 512,
]);

// Increase oidc_access_tokens.scope from 128 to 512
$table->changeColumn('scope', [
    'notnull' => true,
    'length' => 512,
]);
```

### Code Changes
1. **TokenGenerationRequestListener.php** line 83: `128` → `512`
2. **LoginRedirectorController.php** line 437: `128` → `512`
3. **SettingsController.php** line 232: `255` → `511`
4. **DynamicRegistrationController.php** lines 182, 420: `255` → `511`

### Application Changes
1. **Added todo scopes** to default scope lists:
   - `nextcloud_mcp_server/app.py`
   - `tests/conftest.py` (DEFAULT_FULL_SCOPES, DEFAULT_READ_SCOPES, DEFAULT_WRITE_SCOPES)

2. **Skipped obsolete tests**:
   - `test_scope_classification` - Script no longer exists
   - `test_all_tools_classified` - Script no longer exists

## Verification

### Before Fix
- Scope length in database: **128 characters** (truncated)
- Tools visible: **32 out of 90** (35%)
- Missing scopes: `deck`, `tables`, `files`, `sharing`, partial `cookbook:write`

### After Fix
- Scope length in database: **247 characters** (full string)
- Tools visible: **90 out of 90** (100%)
- All scopes present and complete

### Test Results
```bash
$ uv run pytest tests/server/test_scope_authorization.py -v
===== 13 passed, 2 skipped in 22.11s =====
```

All scope authorization tests pass, including:
- ✅ Full access token shows all 90 tools
- ✅ Read-only token filters write tools
- ✅ Write-only token filters read tools
- ✅ JWT consent scenarios work correctly
- ✅ PRM endpoint lists all scopes

## Files Modified

### OIDC App (third_party/oidc/)
- `lib/Migration/Version0015Date20251123100100.php` - Database schema migration
- `lib/Listener/TokenGenerationRequestListener.php` - Token generation scope limit
- `lib/Controller/LoginRedirectorController.php` - OAuth flow scope limit
- `lib/Controller/SettingsController.php` - Client settings scope limit
- `lib/Controller/DynamicRegistrationController.php` - DCR scope limits

### MCP Server
- `nextcloud_mcp_server/app.py` - Added todo scopes to default scopes
- `tests/conftest.py` - Added todo scopes to all scope constants
- `tests/server/test_scope_authorization.py` - Skipped obsolete tests

## Impact
- ✅ All 90 MCP tools now accessible with full access token
- ✅ JWT tokens contain complete scope information
- ✅ No more scope truncation at any layer
- ✅ Database supports up to 512 characters (247 currently used, 265-char margin)
- ✅ Future-proof for adding more scopes

## Current Scope String
```
openid profile email notes:read notes:write calendar:read calendar:write todo:read todo:write contacts:read contacts:write cookbook:read cookbook:write deck:read deck:write tables:read tables:write files:read files:write sharing:read sharing:write
```
**Length**: 247 characters
**Capacity**: 512 characters
**Margin**: 265 characters (107% headroom)
