# ADR-023: OAuth Authorization Server Proxy

## Status

Accepted

## Date

2026-03-02

## Context

When the MCP server operates in OAuth mode (e.g., `mcp-login-flow` profile), MCP clients like Claude Code need to authenticate before calling any tools. The server advertises itself as an OAuth Protected Resource via RFC 9728 (Protected Resource Metadata / PRM), which tells clients where to find the Authorization Server.

### The Problem

The original design used a **pass-through** pattern for Flow 1 (client authentication):

1. PRM at `/.well-known/oauth-protected-resource` pointed `authorization_servers` to Nextcloud's public URL
2. Claude Code performed OIDC discovery on Nextcloud, used DCR to register its own client, and obtained tokens directly from Nextcloud
3. Tokens issued by Nextcloud had Claude Code's `client_id` as the `aud` (audience) claim

This caused an audience mismatch:

```
Token rejected: Missing MCP audience.
Got klehQp8uHCK9fu... (Claude Code's client_id),
need 8ilzB5ZPWr2Qt4... (MCP server's client_id) or http://localhost:8004
```

The `_has_mcp_audience()` check in `unified_verifier.py` correctly requires tokens to contain either the MCP server's `client_id` or its URL as the audience — but tokens obtained directly from Nextcloud by a third-party client will never have that audience.

This meant Claude Code could never authenticate → could never call `nc_auth_provision_access` → Login Flow v2 never triggered → the server was unusable.

### Why Not Just Relax Audience Validation?

Audience validation exists for security (RFC 7519 §4.1.3). Removing it would allow any valid Nextcloud token to access the MCP server, including tokens issued for completely different purposes.

## Decision

Make the MCP server act as its own **OAuth Authorization Server proxy** (intermediary pattern). The MCP server advertises itself as the AS, handles client registration and authorization, but proxies the actual authentication to Nextcloud using its own credentials. This ensures all tokens have the correct audience.

### Flow Overview

```
Client                    MCP Server (AS Proxy)              Nextcloud (IdP)
  |                              |                                |
  |-- POST /oauth/register ----->| ---- proxy DCR --------------->|
  |<---- client_id, etc. --------|<---- client_id, etc. ----------|
  |                              |                                |
  |-- GET /oauth/authorize ----->| (store client params)          |
  |  (client_id, redirect,       | redirect with MCP's client_id  |
  |   code_challenge, state)     |------- GET /authorize -------->|
  |                              |  (MCP client_id, MCP callback) |
  |                              |                                |
  |                              |    [user authenticates]        |
  |                              |                                |
  |                              |<------ code + state -----------|
  |                              | (exchange code server-side)    |
  |                              |------- POST /token ----------->|
  |                              |  (code, MCP client_id+secret)  |
  |                              |<------ NC token (aud=MCP) -----|
  |                              |                                |
  |                              | (generate proxy_code, store    |
  |                              |  mapping to NC token)          |
  |<-- redirect to client -------|                                |
  |    (proxy_code, state)       |                                |
  |                              |                                |
  |-- POST /oauth/token -------->| (verify PKCE, lookup code)    |
  |  (proxy_code, code_verifier) | return stored NC token        |
  |<---- access_token -----------|                                |
  |                              |                                |
  |-- POST /mcp (Bearer token) ->| verify_access_token()         |
  |  (NC token with aud=MCP ✓)   | _has_mcp_audience() → PASS    |
```

### Key Design Decisions

#### 1. PKCE Handling — Local Verification

The MCP server receives the client's `code_challenge` but does **not** forward it to Nextcloud. Instead:

- **Nextcloud side**: MCP server authenticates as a confidential client (`client_id` + `client_secret`), so PKCE is not required
- **Client side**: MCP server verifies PKCE locally when the client exchanges the proxy code at `/oauth/token`

This avoids the impossible situation where the server would need the `code_verifier` to exchange code with Nextcloud but doesn't have it (only the client does).

#### 2. In-Memory Proxy Code Storage

Proxy codes (the authorization codes issued by the AS proxy to clients) use in-memory storage rather than SQLite because:

- They have a 60-second TTL
- They are single-use (deleted on exchange)
- They only exist during the brief OAuth flow
- The MCP server is single-instance

#### 3. PRM Points to MCP Server

The `authorization_servers` field in the PRM response now points to the MCP server URL instead of Nextcloud's public URL. This is what triggers the entire proxy flow — clients discover the MCP server as their AS.

#### 4. DCR Proxy

Client registration requests at `/oauth/register` are proxied to Nextcloud's DCR endpoint. The resulting `client_id` is stored in the local `ClientRegistry` so that `/oauth/authorize` can validate it. The client receives the same DCR response it would get from Nextcloud directly.

## Alternatives Considered

### 1. Relax Audience Validation

Remove `_has_mcp_audience()` check entirely. **Rejected**: Violates RFC 7519 security model.

### 2. Client Pre-Registration

Require clients to register directly with Nextcloud and configure the MCP server with their `client_id`. **Rejected**: Poor UX, doesn't work with DCR-based clients like Claude Code.

### 3. Token Exchange (RFC 8693)

The MCP server could accept any Nextcloud token and exchange it for one with the correct audience. **Rejected**: Nextcloud's OIDC app doesn't support RFC 8693 token exchange. This was already explored in ADR-005.

### 4. Custom Audience Configuration

Add configuration to accept specific external `client_id` values as valid audiences. **Rejected**: Requires manual configuration per client, doesn't scale with DCR.

## New Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/.well-known/oauth-authorization-server` | GET | RFC 8414 AS metadata |
| `/oauth/authorize` | GET | Authorization (modified: intermediary, not pass-through) |
| `/oauth/token` | POST | Token exchange (proxy codes + refresh token proxy) |
| `/oauth/register` | POST | DCR proxy to Nextcloud |

## Files Modified

| File | Changes |
|------|---------|
| `nextcloud_mcp_server/auth/oauth_routes.py` | New: `oauth_as_metadata`, `oauth_register_proxy`, `oauth_token_endpoint`, `_oauth_callback_as_proxy`. Modified: `oauth_authorize` (intermediary pattern), `oauth_callback` (AS proxy routing) |
| `nextcloud_mcp_server/app.py` | New routes, PRM `authorization_servers` → MCP server URL, `app.state.supported_scopes` |
| `nextcloud_mcp_server/auth/client_registry.py` | New: `register_proxy_client()`, wildcard scope support |

## Consequences

### Positive

- Tokens always have the correct audience — `_has_mcp_audience()` passes
- Works with any MCP client that implements RFC 9728 (PRM) discovery
- No changes needed to Nextcloud's OIDC configuration
- DCR still works transparently (clients register via proxy)
- Existing Flow 2 (resource provisioning) and browser login are unaffected

### Negative

- MCP server is now stateful during the OAuth flow (in-memory proxy codes)
- Extra network hop for token exchange (MCP server → Nextcloud → back)
- Token refresh requires proxying through the MCP server
- Single-instance limitation for proxy code storage (acceptable for current deployment model)

### Risks

- In-memory proxy codes are lost on server restart (mitigated by 60s TTL — user just retries)
- Discovery endpoint fetch during OAuth flow adds latency (could be cached)

## References

- [RFC 8414 — OAuth 2.0 Authorization Server Metadata](https://tools.ietf.org/html/rfc8414)
- [RFC 9728 — OAuth 2.0 Protected Resource Metadata](https://tools.ietf.org/html/rfc9728)
- [RFC 7636 — PKCE](https://tools.ietf.org/html/rfc7636)
- [RFC 7591 — Dynamic Client Registration](https://tools.ietf.org/html/rfc7591)
- ADR-004 — MCP Application OAuth (progressive consent architecture)
- ADR-005 — Token Audience Validation
