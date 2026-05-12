# Authentication Flows by Deployment Mode

This document provides a unified reference for the auth flows in each supported deployment mode. For configuration details, see [Authentication](authentication.md). For Login Flow v2 architecture and setup, see [Login Flow v2](login-flow-v2.md).

## Quick Reference Matrix

| Mode | Client → MCP → NC | Background Sync | Astrolabe (hosted UI) → MCP |
|------|-------------------|-----------------|------------------------------|
| [Single-User BasicAuth](#1-single-user-basicauth) | Embedded credentials | Same credentials | N/A |
| [Multi-User BasicAuth](#2-multi-user-basicauth) | Header pass-through | Stored app password (optional) | OAuth Bearer token |
| [Login Flow v2](#3-login-flow-v2) | OAuth → MCP, app pwd → NC | Stored app password | OAuth Bearer token |

## Communication Patterns

This document covers three distinct communication patterns:

1. **MCP Client → MCP Server → Nextcloud**: Interactive tool calls initiated by users through MCP clients (Claude Desktop, claude.ai, custom clients).
2. **MCP Server → Nextcloud**: Background operations like vector sync that run without user interaction.
3. **Astrolabe → MCP Server**: Astrolabe app backend communication for settings UI and unified search.

---

## Deployment Modes

### 1. Single-User BasicAuth

**Use Case:** Personal Nextcloud instance, local development, single-user deployments.

#### MCP Client → MCP Server → Nextcloud

```
MCP Client                    MCP Server                   Nextcloud
    │                             │                            │
    │── MCP Request ─────────────▶│                            │
    │   (no auth required)        │                            │
    │                             │── HTTP + BasicAuth ───────▶│
    │                             │   Authorization: Basic     │
    │                             │   (embedded credentials)   │
    │                             │◀── API Response ───────────│
    │◀── Tool Result ─────────────│                            │
```

**Key characteristics:**
- Credentials embedded in server configuration (`NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`)
- Single shared `NextcloudClient` created at startup
- No MCP-level authentication required (server trusts local clients)
- All requests use the same Nextcloud user

**Implementation:** `context.py` — returns the shared client from lifespan context

#### Background Sync

Uses the same embedded credentials as interactive requests. The background job accesses Nextcloud with the configured username/password.

#### Astrolabe Integration

Not applicable — Astrolabe is only used in multi-user deployments where users need personal settings and per-user state.

---

### 2. Multi-User BasicAuth

**Use Case:** Internal deployment where users provide their own Nextcloud credentials via HTTP headers.

#### MCP Client → MCP Server → Nextcloud

```
MCP Client                    MCP Server                   Nextcloud
    │                             │                            │
    │── MCP Request ─────────────▶│                            │
    │   Authorization: Basic      │                            │
    │   (user credentials)        │                            │
    │                             │── BasicAuthMiddleware ────▶│
    │                             │   Extracts credentials     │
    │                             │                            │
    │                             │── HTTP + BasicAuth ───────▶│
    │                             │   (pass-through)           │
    │                             │◀── API Response ───────────│
    │◀── Tool Result ─────────────│                            │
```

**Key characteristics:**
- `BasicAuthMiddleware` extracts credentials from the `Authorization: Basic` header
- Credentials passed through to Nextcloud (not stored)
- Client created per-request from extracted credentials
- Stateless — no credential storage between requests

#### Background Sync (Optional)

If users provision an app password (via Astrolabe or `nc_auth_provision_access`), the server can run background jobs on their behalf:

```
Astrolabe                     MCP Server                   Nextcloud
    │                             │                            │
    │── Store app password ──────▶│                            │
    │   (via management API)      │                            │
    │                             │ [Encrypt + persist locally]│
    │                             │  (SQLite, Fernet)          │
    │◀── Confirmation ────────────│                            │
    │                             │                            │
    │         [Background job]    │                            │
    │                             │── Retrieve app password ──▶│
    │                             │── HTTP + BasicAuth ───────▶│
    │                             │◀── API Response ───────────│
```

**Requirements:** `TOKEN_ENCRYPTION_KEY`, `TOKEN_STORAGE_DB`.

#### Astrolabe → MCP Server

```
Astrolabe                     MCP Server                   OIDC Provider
    │                             │                            │
    │── OAuth Flow ──────────────▶│◀── Token from IdP ────────▶│
    │   (user initiates)          │                            │
    │                             │                            │
    │── Bearer Token ────────────▶│                            │
    │   (management API calls)    │                            │
    │                             │── Validate via JWKS ──────▶│
    │                             │   (or introspection)       │
    │◀── API Response ────────────│                            │
```

**Key characteristics:**
- Astrolabe has its own OAuth client registered with the IdP (Nextcloud OIDC by default; Keycloak / Cognito / etc. when configured via `OIDC_DISCOVERY_URL`)
- Tokens are validated by the MCP server using the IdP's JWKS (Nextcloud OIDC's JWKS by default; whichever IdP is configured otherwise)
- Authorization check: `token.sub == requested_resource_owner`
- The same JWKS-based validation path applies under [Login Flow v2](#3-login-flow-v2) — the MCP server is an OIDC relying party of the configured IdP in both modes; Login Flow v2 only changes the MCP→Nextcloud credential leg (per-user app passwords).

---

### 3. Login Flow v2

**Use Case:** Hosted multi-user deployments, OAuth-based MCP clients (claude.ai, Astrolabe Cloud), production. Recommended for any setup where MCP clients shouldn't handle Nextcloud credentials directly.

This mode replaces the previously-supported "OAuth Single-Audience" and "OAuth Token Exchange" modes, both of which required upstream Nextcloud patches that were never merged. See [ADR-022](ADR-022-deployment-mode-consolidation.md) for the rationale.

#### MCP Client → MCP Server → Nextcloud (steady state)

```
MCP Client                    MCP Server                   Nextcloud
    │                             │                            │
    │── Bearer Token ────────────▶│                            │
    │   (issued by configured IdP,│                            │
    │    per-app scopes)          │                            │
    │                             │── Validate scopes ─────────│
    │                             │   (@require_scopes)        │
    │                             │                            │
    │                             │── Lookup user's            │
    │                             │   stored app password      │
    │                             │                            │
    │                             │── HTTP + BasicAuth ───────▶│
    │                             │   Authorization: Basic     │
    │                             │   (per-user app password)  │
    │                             │◀── API Response ───────────│
    │◀── Tool Result ─────────────│                            │
```

**Key characteristics:**
- MCP client authenticates to MCP server via OAuth 2.1 + PKCE
- MCP server is an **OIDC relying party of a configurable IdP** (Nextcloud OIDC by default; Keycloak, AWS Cognito, etc. via `OIDC_DISCOVERY_URL`) + an OAuth facade for MCP clients. RFC 7591 DCR is used to register the MCP-client side; the server's own RP credentials come from `NEXTCLOUD_OIDC_CLIENT_ID/SECRET` (generic OIDC creds), with DCR fallback. Tokens are signed by the chosen IdP and validated against that IdP's JWKS.
- Per-app scopes (e.g. `notes.read`, `talk.read`, `files.write`) gate tool access — see [Login Flow v2 → Scope Reference](login-flow-v2.md#scope-reference) for the full list
- Per-user app password obtained via Login Flow v2 (Nextcloud-specific protocol, used regardless of which IdP authenticated the client) and stored encrypted in SQLite
- App passwords appear in Nextcloud's **Settings → Security → Devices & Sessions** and are user-revocable

#### First-Use Provisioning (one-time per user)

```
MCP Client                    MCP Server                   Nextcloud
    │                             │                            │
    │── Bearer Token + request ──▶│                            │
    │                             │   No stored app password   │
    │                             │                            │
    │◀── Elicit URL or 401 ───────│                            │
    │   "Visit <login-url>"       │                            │
    │                             │── POST /index.php/login/v2▶│
    │                             │◀── login_url, poll_token ──│
    │                             │                            │
    │   User opens login_url in browser, authenticates, "Grant"│
    │   ──────────────────────────────────────────────────────▶│
    │                             │                            │
    │                             │── Poll endpoint (bg) ─────▶│
    │                             │◀── loginName, appPassword ─│
    │                             │                            │
    │                             │── Encrypt + store          │
    │                             │   in tokens.db             │
    │                             │                            │
    │── Retry request ───────────▶│── Basic Auth as above ────▶│
```

#### Background Sync

Uses the same per-user app password retrieved from encrypted storage. No token refresh needed — Nextcloud app passwords don't expire (until the user revokes them).

```
                              MCP Server                   Nextcloud
                                  │                            │
    [Background job starts]       │                            │
                                  │── Retrieve app password ──▶│
                                  │   (per user, from SQLite)  │
                                  │                            │
                                  │── HTTP + BasicAuth ───────▶│
                                  │◀── API Response ───────────│
```

#### Astrolabe → MCP Server

Same as Multi-User BasicAuth — see [Astrolabe → MCP Server](#astrolabe--mcp-server) above.

---

## Configuration Quick Reference

### Single-User BasicAuth
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=<app-password>
```

### Multi-User BasicAuth
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
MCP_DEPLOYMENT_MODE=multi_user_basic

# Optional: app-password storage for background sync
TOKEN_ENCRYPTION_KEY=<fernet-key>
TOKEN_STORAGE_DB=/app/data/tokens.db
```

### Login Flow v2
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
MCP_DEPLOYMENT_MODE=login_flow

# Required for app-password storage
TOKEN_ENCRYPTION_KEY=<fernet-key>
TOKEN_STORAGE_DB=/app/data/tokens.db

# Public URLs (for browser redirects)
NEXTCLOUD_MCP_SERVER_URL=https://mcp.example.com
NEXTCLOUD_PUBLIC_ISSUER_URL=https://nextcloud.example.com
```

See [Login Flow v2](login-flow-v2.md) for full setup, scope reference, and troubleshooting.

---

## Related Documentation

- [Authentication](authentication.md) — mode comparison and selection
- [Login Flow v2](login-flow-v2.md) — multi-user setup details
- [Configuration](configuration.md) — environment variable reference
- [ADR-022](ADR-022-deployment-mode-consolidation.md) — design rationale for mode consolidation
