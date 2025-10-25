# DCR Client Deletion Investigation

## Summary

✅ **RESOLVED** - As of 2025-10-24, Dynamic Client Registration (DCR) via RFC 7591 **and** RFC 7592 client deletion now work correctly in Nextcloud's OIDC server!

**Historical Note**: This document was originally created to investigate DCR deletion failures. The issue has been resolved by merging two feature branches (`feature/user-consent-complete` and `feature/dcr-jwt-scopes`) that implement RFC 7592 support.

## Resolution Summary (2025-10-24)

### What Now Works ✅
- **Client Registration** (RFC 7591): Successfully creates OAuth clients with custom scopes and token types
- **Registration Access Token**: ✅ Now included in registration response per RFC 7592
- **Registration Client URI**: ✅ Now included in registration response per RFC 7592
- **Client Deletion** (RFC 7592): ✅ Now works with Bearer token authentication
- **Token Acquisition**: Registered clients can obtain access tokens via authorization code flow
- **API Access**: Tokens work correctly for accessing Nextcloud APIs

### Test Evidence

The test `test_new_dcr_registration_includes_access_token` in `tests/server/oauth/test_dcr_new_implementation.py` confirms:

**Registration Response:**
```json
{
  "client_id": "wynkPur15ibby0Ma2FUOMyv4JdmtxqlRepvGmERrE36RYmquuExma1srAgDG1rKZ",
  "client_secret": "agaZU3WdffOy4o6TS4vZ...",
  "registration_access_token": "uKycqheAzw2UMZUL58Ir...",
  "registration_client_uri": "http://localhost:8080/apps/oidc/register/wynkPur15ibby0Ma2FUOMyv4JdmtxqlRepvGmERrE36RYmquuExma1srAgDG1rKZ",
  ...
}
```

**Deletion Test:**
- Endpoint: `DELETE /apps/oidc/register/{client_id}`
- Authentication: `Authorization: Bearer {registration_access_token}`
- Response: **204 No Content** ✅

### Implementation Details

The resolution required:
1. Merging `feature/user-consent-complete` and `feature/dcr-jwt-scopes` branches
2. Adding missing classes to composer autoload files:
   - `OCA\OIDCIdentityProvider\Db\RegistrationToken`
   - `OCA\OIDCIdentityProvider\Db\RegistrationTokenMapper`
   - `OCA\OIDCIdentityProvider\Service\RegistrationTokenService`
3. Fixing method calls in `DynamicRegistrationController.php`:
   - Changed `findByClientId()` to `getByClientId()` for RedirectUriMapper
   - Removed logout redirect URI deletion (not client-specific in schema)
4. Database migration applied automatically (`oc_oidc_reg_tokens` table created)

### Files Modified

- `third_party/oidc/composer/composer/autoload_classmap.php` - Added 3 new class mappings
- `third_party/oidc/composer/composer/autoload_static.php` - Added 3 new class mappings
- `third_party/oidc/lib/Controller/DynamicRegistrationController.php` - Fixed deletion logic
- `third_party/oidc/lib/Db/LogoutRedirectUriMapper.php` - Added `deleteByClientId()` method

## Technical Details

### Registration Response Analysis

When registering a client via POST to `/apps/oidc/register`, the response includes:

```json
{
  "client_name": "DCR Lifecycle Test Client",
  "client_id": "eVdV1obTHUhtQiBOLnDcOucZE3sQA6J7JgzsDFsnpgzLkWSNEPXHJbpSfjLUU5ot",
  "client_secret": "iqNeH5inrdTPh6hYGOmvlML7SWqHPHpMZp9CQlNHNnKGf6VZ8pSeaSC1EBrDRmyd",
  "redirect_uris": ["http://localhost:8081"],
  "token_endpoint_auth_method": "client_secret_post",
  "response_types": ["code"],
  "grant_types": ["authorization_code"],
  "id_token_signed_response_alg": "RS256",
  "application_type": "web",
  "client_id_issued_at": 1761286688,
  "client_secret_expires_at": 1761290288,
  "scope": "openid profile email notes:read",
  "token_type": "Bearer"
}
```

**Missing:** `registration_access_token` and `registration_client_uri`

### Deletion Attempt Analysis

Attempting DELETE to `/apps/oidc/register/{client_id}` with various authentication methods:

#### Method 1: HTTP Basic Auth
- **Authentication**: HTTP Basic Auth with `client_id` as username, `client_secret` as password
- **Response**: 401 Unauthorized
- **Response Body**: `{"message":""}`

#### Method 2: Credentials in JSON Body
- **Authentication**: JSON body with `client_id` and `client_secret`
- **Response**: N/A (httpx.AsyncClient.delete() doesn't support `json` parameter)

#### Method 3: Credentials in Query Parameters
- **Authentication**: Query params `?client_id=...&client_secret=...`
- **Response**: 500 Internal Server Error (server-side exception when parsing query params)

#### Method 4: No Authentication (Baseline)
- **Authentication**: None
- **Response**: 401 Unauthorized
- **Response Body**: `{"error":"invalid_client","error_description":"Client authentication failed."}`

**Conclusion**: The 401 error occurs with HTTP Basic Auth (the standard RFC 7592 method). Query parameters cause a 500 error (not supported). No authentication returns 401 as expected.

### RFC 7592 Requirements (Not Met)

According to [RFC 7592 Section 3](https://www.rfc-editor.org/rfc/rfc7592.html#section-3), the registration endpoint MUST return:

1. **`registration_access_token`**: A token for subsequent management operations (read, update, delete)
2. **`registration_client_uri`**: The URI for managing this client

The client delete request should then use:
```http
DELETE /apps/oidc/register/{client_id}
Authorization: Bearer {registration_access_token}
```

## Root Cause Analysis

### Possible Causes

1. **Nextcloud OIDC Server Implementation Gap**
   - The OIDC server (likely based on third-party library) may not fully implement RFC 7592
   - Registration (RFC 7591) is implemented, but management operations (RFC 7592) are not

2. **Middleware Blocking**
   - Nextcloud middleware may be blocking unauthenticated DELETE requests to `/apps/oidc/*`
   - The 401 error suggests authentication is being checked but failing

3. **Missing Feature**
   - Client deletion may simply not be implemented in the current OIDC app version
   - The endpoint exists but returns 401 regardless of credentials

## Impact on Test Fixtures

### Current Fixture Behavior

The `shared_oauth_client_credentials` and `shared_jwt_oauth_client_credentials` fixtures in `tests/conftest.py` (lines 947-1112) attempt to clean up registered clients using:

```python
success = await delete_client(
    nextcloud_url=nextcloud_host,
    client_id=client_id,
    client_secret=client_secret,
)
```

This cleanup **always fails** (returns `False`) due to the 401 error, but the failure is handled gracefully with a warning:

```python
except Exception as e:
    logger.warning(
        f"Error cleaning up shared OAuth client {client_id[:16]}...: {e}"
    )
```

### Consequences

1. **OAuth Clients Accumulate**: Every test session registers 2 OAuth clients that are never deleted
2. **No Functional Impact**: Tests continue to work because:
   - Clients have 1-hour expiration (`client_secret_expires_at`)
   - New clients are registered for each session
   - Old clients expire automatically
3. **Database Bloat**: Over time, the `oc_oauth2_clients` table may accumulate expired clients

## Recommendations

### Short Term (Current Approach)

1. **Keep Current Warning-Based Approach**: The fixtures already handle deletion failure gracefully
2. **Document Expected Behavior**: Add comments explaining that deletion is expected to fail
3. **Accept Client Accumulation**: Rely on automatic expiration (1 hour)

### Long Term (If DCR Deletion Needed)

1. **Check Nextcloud OIDC App Version**: Verify if newer versions support RFC 7592 deletion
2. **File Bug Report**: Report missing `registration_access_token` to Nextcloud OIDC project
3. **Alternative Cleanup**: Use Nextcloud admin API to delete OAuth clients directly
   - Requires admin credentials
   - Bypass OIDC app's DCR endpoint
   - Example: `occ oauth:clients:delete {client_id}`

### Recommended Fixture Update

```python
@pytest.fixture(scope="session")
async def shared_oauth_client_credentials(anyio_backend, oauth_callback_server):
    """
    ... existing docstring ...

    Note:
        Client deletion via RFC 7592 is not supported by Nextcloud OIDC server
        (missing registration_access_token). Clients will expire after 1 hour
        automatically. Manual cleanup via admin API may be needed in production.
    """
    # ... registration code ...

    yield (...)

    # Cleanup: Attempt deletion (expected to fail due to RFC 7592 limitation)
    try:
        logger.info(f"Attempting cleanup of shared OAuth client: {client_id[:16]}...")
        success = await delete_client(
            nextcloud_url=nextcloud_host,
            client_id=client_id,
            client_secret=client_secret,
        )
        if success:
            logger.info(f"✅ Successfully deleted client: {client_id[:16]}...")
        else:
            logger.warning(
                f"⚠️  Client deletion not supported by Nextcloud OIDC server. "
                f"Client {client_id[:16]}... will expire automatically in 1 hour."
            )
    except Exception as e:
        logger.warning(
            f"⚠️  Error during client cleanup (expected): {e}. "
            f"Client will expire automatically."
        )
```

## Test File Status

Created `tests/server/oauth/test_dcr_lifecycle.py` with 4 comprehensive tests:

1. ✅ `test_dcr_register_and_delete_lifecycle` - Documents full lifecycle (fails at deletion step as expected)
2. ✅ `test_dcr_delete_with_wrong_credentials` - Verifies authentication behavior
3. ✅ `test_dcr_delete_nonexistent_client` - Tests error handling
4. ✅ `test_dcr_deletion_is_idempotent` - Tests repeated deletion attempts

**All tests currently fail at the deletion step**, which is expected given the RFC 7592 limitation.

## Next Steps

1. **Update fixture comments** to document expected deletion failure
2. **Mark deletion tests as expected failures** using `@pytest.mark.xfail`
3. **Consider removing deletion tests** if they don't provide value (since deletion doesn't work)
4. **Investigate Nextcloud admin API** as alternative cleanup method for CI/CD environments
5. **Monitor Nextcloud OIDC app updates** for RFC 7592 support

## References

- [RFC 7591 - OAuth 2.0 Dynamic Client Registration Protocol](https://www.rfc-editor.org/rfc/rfc7591.html)
- [RFC 7592 - OAuth 2.0 Dynamic Client Registration Management Protocol](https://www.rfc-editor.org/rfc/rfc7592.html)
- Nextcloud OIDC App: Check `docker-compose.yml` for app location
- Test Evidence: `tests/server/oauth/test_dcr_lifecycle.py` line 254-256 (401 response details)
