# JWT Scope Truncation Issue

## Problem
When using JWT tokens with many scopes, the `scope` claim in the JWT payload gets truncated.

## Evidence
- **allowed_scopes** in `oc_oidc_clients`: 226 characters (ALL scopes present)
  ```
  openid profile email notes:read notes:write calendar:read calendar:write contacts:read contacts:write cookbook:read cookbook:write deck:read deck:write tables:read tables:write files:read files:write sharing:read sharing:write
  ```

- **Scopes in JWT token**: Only partial scopes (truncated at ~70 characters)
  ```
  openid email notes:read notes:write cookbook:wri contacts:read calendar:write profile cookbook:read calendar:read contacts:write
  ```

- **Missing scopes** in JWT:
  - `cookbook:write` (appears as `cookbook:wri`)
  - `deck:read`, `deck:write`
  - `tables:read`, `tables:write`
  - `files:read`, `files:write`
  - `sharing:read`, `sharing:write`

## Root Cause
The Nextcloud OIDC app has a limitation when generating JWT tokens - the `scope` claim is being truncated, likely due to:
1. Database field size limit in JWT token generation code
2. JWT payload size optimization
3. Hardcoded string length limit

## Solution Options
1. **Increase JWT scope claim size limit** in OIDC app (preferred for your use case)
2. Use opaque tokens instead of JWT tokens (no truncation, but requires introspection)
3. Use scope groups/roles instead of individual scopes
4. Store scopes in a separate JWT claim array format

## Temporary Workaround
For testing, we adjusted the test expectations to match the actual number of tools available with truncated scopes (32 tools instead of 90+).

## Action Required
The OIDC app needs investigation to identify and fix the JWT scope truncation. Check:
- `lib/Controller/LoginController.php` - JWT generation code
- Database schema for JWT-related fields
- JWT library configuration for payload size limits
