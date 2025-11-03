Excellent and incredibly thorough work on ADR-004. It outlines a robust, secure, and modern approach to federated authentication that aligns with industry best practices. The Progressive Consent architecture with dual OAuth flows is the right direction for a system with these requirements.

Here is a review of the current implementation in light of the architecture proposed in the ADR.

### High-Level Assessment

The project is in a good state, with a clear vision for its authentication architecture. The current implementation provides a backward-compatible "Hybrid Flow" while also containing the scaffolding for the target "Progressive Consent" flow. The hybrid flow is well-tested, which is a great foundation.

The following points are intended to help bridge the gap between the current implementation and the final vision outlined in ADR-004.

### Critical Security Review

#### 1. Missing Token Audience (`aud`) Validation

This is the most critical issue. The `require_scopes` decorator currently checks for scopes but does not validate the `audience` (`aud` claim) of the incoming JWT.

*   **Risk:** This creates a "confused deputy" vulnerability. An access token issued for a different application could be used to access the MCP server, as long as the scope names happen to match.
*   **ADR Reference:** The ADR correctly identifies this and proposes an `MCPTokenVerifier` that validates `aud: "mcp-server"`.
*   **Recommendation:** Implement the audience validation as a central part of your token verification middleware. An incoming token should be rejected immediately if its audience is not `mcp-server`. This check should happen before any tool-specific scope checks.

### Architecture and Implementation Review

#### 2. Progressive Consent Flow is Untested

The code for the Progressive Consent flow (behind the `ENABLE_PROGRESSIVE_CONSENT` flag) exists in `oauth_routes.py` and `oauth_tools.py`. However, there are no integration tests to validate it.

*   **Risk:** Given the complexity of OAuth flows, it's likely there are bugs in the untested implementation.
*   **Recommendation:** Create a new test file, `test_adr004_progressive_flow.py`, that uses Playwright to test the dual-flow architecture end-to-end:
    1.  **Flow 1:** A test MCP client authenticates directly with the IdP to get an `mcp-server` token.
    2.  **Provisioning Check:** The test verifies that calling a Nextcloud tool fails with a `ProvisioningRequiredError`.
    3.  **Flow 2:** The test calls the `provision_nextcloud_access` tool and automates the second OAuth flow to grant the server offline access.
    4.  **Tool Execution:** The test verifies that Nextcloud tools can now be successfully called.

#### 3. Inconsistent Authorization URL Generation

There is duplicated and inconsistent logic for generating the IdP authorization URL.

*   **Location 1:** `oauth_tools.py` in `generate_oauth_url_for_flow2` hardcodes the authorization endpoint path.
*   **Location 2:** `oauth_routes.py` in `oauth_authorize_nextcloud` correctly uses the OIDC discovery document to find the `authorization_endpoint`.
*   **Risk:** The hardcoded path is brittle and will break with IdPs that use different endpoint paths (like Keycloak).
*   **Recommendation:** Consolidate this logic. The `provision_nextcloud_access` tool should not build the URL itself. Instead, it should return a URL pointing to the MCP server's own `/oauth/authorize-nextcloud` endpoint. This endpoint (which you've already created as `oauth_authorize_nextcloud` in `oauth_routes.py`) can then be the single source of truth for generating the IdP redirect.

#### 4. Poor User Experience due to Missing Token Refresh

The `/oauth/token` endpoint does not implement the `refresh_token` grant type. This means that when the client's `mcp-server` access token expires (e.g., after one hour), the user must go through the entire browser-based login flow again.

*   **Risk:** This creates a frustrating user experience, especially for long-lived desktop clients.
*   **ADR Reference:** A proper Flow 1 should result in the MCP client receiving both an access token and a refresh token from the IdP.
*   **Recommendation:**
    1.  Ensure the IdP is configured to issue refresh tokens to the MCP client for Flow 1.
    2.  The MCP client should securely store this refresh token.
    3.  The client should use the refresh token to get new `mcp-server` access tokens directly from the IdP, without involving the MCP server or the user. The MCP server should not be involved in the client's session management with the IdP.

### Summary

The project is on the right track. The ADR is a solid plan, and the initial implementation is a good starting point.

My recommendations in order of priority are:

1.  **Implement Audience Validation** to close the security gap.
2.  **Add Integration Tests** for the Progressive Consent flow.
3.  **Refactor the client-side token refresh** to improve user experience.
4.  **Consolidate the URL generation** logic to fix the inconsistency.

Addressing these points will align the implementation with the excellent vision in ADR-004 and result in a secure, robust, and user-friendly system.