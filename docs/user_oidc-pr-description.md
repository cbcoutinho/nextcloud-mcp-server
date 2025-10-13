# Fix Bearer Token Authentication Causing Session Logout

## Problem

Bearer token authentication with OIDC fails for app-specific APIs (like Notes, Calendar, etc.) with `401 Unauthorized` errors, even though the same Bearer token works fine for OCS APIs (like `/ocs/v2.php/cloud/capabilities`).

### Root Cause

When using Bearer token authentication:

1. ✅ Bearer token validation successfully authenticates the user
2. ✅ A session is created for the authenticated user
3. ❌ **Nextcloud's `CORSMiddleware` detects the logged-in session but no CSRF token**
4. ❌ **`CORSMiddleware` calls `$this->session->logout()` to prevent CSRF attacks**
5. ❌ The logout invalidates the session, breaking the API request with 401 Unauthorized

This occurs because app-specific APIs (Notes, Calendar, etc.) use the `@CORS` annotation, which triggers the `CORSMiddleware` security checks. The OCS APIs don't have this annotation, which is why they work correctly.

### Error Logs

```
[TokenInvalidatedListener] Could not find the OIDC session related with an invalidated token
Session token invalidated before logout
Logging out
```

## Solution

Set the `app_api` session flag during Bearer token authentication. This instructs `CORSMiddleware` to skip the CSRF check and logout logic, as the authentication is API-based rather than session-based.

This is the same mechanism used by Nextcloud's [AppAPI framework](https://github.com/cloud-py-api/app_api) for external application authentication.

### Changes

The fix adds `$this->session->set('app_api', true);` before all successful Bearer token authentication return statements in `lib/User/Backend.php`:

- **Line 243**: After OIDC Identity Provider validation
- **Line 310**: After auto-provisioning with bearer provisioning
- **Line 315**: After existing user authentication
- **Line 337**: After LDAP user sync

## Testing

Tested with the [nextcloud-mcp-server](https://github.com/cccs-nik/nextcloud-mcp-server) project's integration tests:

### Before Fix
```
✅ Capabilities endpoint (OCS API) - 200 OK
❌ Notes API listing - 401 Unauthorized
❌ Notes API create - 401 Unauthorized
```

### After Fix
```
✅ Capabilities endpoint (OCS API) - 200 OK
✅ Notes API listing - 200 OK
✅ Notes API create - 200 OK
✅ Notes API delete - 200 OK
```

All OAuth Bearer token operations now work correctly across all Nextcloud APIs without session invalidation.

## Configuration

This fix works with the standard Bearer token validation configuration:

```php
// config.php
'user_oidc' => [
    'oidc_provider_bearer_validation' => true,
],
```

And in the OIDC Identity Provider app:
```bash
php occ config:app:set oidc dynamic_client_registration --value='true'
```

## Impact

This fix enables proper Bearer token authentication for:
- All Nextcloud app APIs (Notes, Calendar, Contacts, etc.)
- External applications using OAuth 2.0 / OpenID Connect
- MCP servers and other API integrations
- Any application using the `Authorization: Bearer` header

## Related Files

- `lib/User/Backend.php` - Modified to set `app_api` flag
- `/server/lib/private/AppFramework/Middleware/Security/CORSMiddleware.php` - Contains the CSRF/logout logic that this bypasses

## References

- [Nextcloud CORS Middleware](https://github.com/nextcloud/server/blob/master/lib/private/AppFramework/Middleware/Security/CORSMiddleware.php)
- [Nextcloud AppAPI](https://github.com/cloud-py-api/app_api)
- [OpenID Connect Bearer Token Usage](https://openid.net/specs/openid-connect-core-1_0.html#TokenUsage)
