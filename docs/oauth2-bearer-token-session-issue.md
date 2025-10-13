# Root Cause Analysis: OAuth2 Bearer Token Session Invalidation

## Problem
Bearer token authentication fails for app-specific APIs (like Notes) with 401 Unauthorized, even though it works for OCS APIs (capabilities).

## Root Cause
The CORSMiddleware in Nextcloud server is logging out the session created by Bearer token authentication:

```
/home/chris/Software/server/lib/private/AppFramework/Middleware/Security/CORSMiddleware.php:84
$this->session->logout();
```

### Why Session is Logged Out
1. Notes API has @CORS annotation
2. Bearer auth via user_oidc creates a logged-in session
3. Request has NO CSRF token
4. Request has NO AppAPI auth flag
5. Request has NO PHP_AUTH_USER/PHP_AUTH_PW (basic auth)
6. Therefore CORSMiddleware calls logout()

### Log Evidence
```
{"message":"[TokenInvalidatedListener] Could not find the OIDC session related with an invalidated token"}
```

Token validated successfully, then immediately invalidated by session logout.

## Token Type Investigation (Opaque vs JWT)
- **Finding**: Token type (opaque vs JWT) does NOT affect the issue
- **Reason**: Session invalidation happens AFTER successful token validation
- Both opaque and JWT tokens validate correctly via TokenValidationRequestEvent
- The logout happens in CORSMiddleware, not in token validation

## ✅ SOLUTION (Tested & Working)

### Option A: Set AppAPI Flag for Bearer Auth ✅
**Status**: Successfully tested and verified working

Modified user_oidc `Backend.php` `getCurrentUserId()` method to set the `app_api` session flag before returning the user ID:

```php
$this->session->set('app_api', true);
```

This bypasses CORS middleware's logout logic at line 81-82 by setting the same flag used by Nextcloud's AppAPI framework.

### Implementation
The flag is added before all successful Bearer token authentication return statements in `/var/www/html/custom_apps/user_oidc/lib/User/Backend.php`:

- Line ~243: After OIDC provider validation
- Line ~310: After auto-provisioning with bearer provisioning
- Line ~315: After existing user authentication
- Line ~337: After LDAP user sync

### Test Results
All OAuth Bearer token operations now work correctly:

✅ **Capabilities endpoint** (OCS API) - 200 OK
✅ **Notes API listing** - 200 OK
✅ **Notes API create** - 200 OK (created note 112)
✅ **Notes API delete** - 200 OK (deleted note 112)

No session invalidation occurs, and all API operations complete successfully.

### Patch File
See `patches/user_oidc-bearer-auth-app-api-flag.patch` for the exact changes.

## Alternative Solutions (Not Tested)

### Option B: Avoid Creating Full Session for Bearer Auth
Bearer token auth should not create a full session that triggers CORS middleware checks. This would require deeper architectural changes.

### Option C: Add CSRF Exemption
Modify CORSMiddleware to exempt Bearer token authenticated requests from CSRF check. This would require changes to Nextcloud core.

### Option D: Use Basic Auth Headers
Set PHP_AUTH_USER/PHP_AUTH_PW server variables during Bearer auth so CORSMiddleware can re-authenticate. This could have security implications.

## Recommendations

### Short-term (Current Implementation)
The `app_api` flag solution works correctly and follows Nextcloud's existing pattern for API authentication. This is the recommended approach for immediate use.

### Long-term (Upstream Contribution)
Consider submitting this fix to the upstream user_oidc project as it enables proper Bearer token authentication for all Nextcloud APIs, not just OCS endpoints.

## Files Involved
- `/home/chris/Software/user_oidc/lib/User/Backend.php` (getCurrentUserId) - **MODIFIED**
- `/home/chris/Software/server/lib/private/AppFramework/Middleware/Security/CORSMiddleware.php` (logout logic)
- `/home/chris/Software/user_oidc/lib/Listener/TokenInvalidatedListener.php` (cleanup handler)

## Testing
Run the OAuth interactive test to verify:
```bash
uv run pytest tests/integration/test_oauth_interactive.py -v
```
