# Authentication Flows by Deployment Mode

This document provides a unified reference for authentication flows across all deployment modes. For configuration details, see [Authentication](authentication.md). For OAuth protocol details, see [OAuth Architecture](oauth-architecture.md).

## Quick Reference Matrix

| Mode | Client → MCP → NC | Background Sync | Astrolabe → MCP |
|------|-------------------|-----------------|-----------------|
| [Single-User BasicAuth](#1-single-user-basicauth) | Embedded credentials | Same credentials | N/A |
| [Multi-User BasicAuth](#2-multi-user-basicauth) | Header pass-through | App password (optional) | Bearer token |
| [OAuth Single-Audience](#3-oauth-single-audience-default) | Multi-audience token | Refresh token exchange | Bearer token |
| [OAuth Token Exchange](#4-oauth-token-exchange-rfc-8693) | RFC 8693 exchange | Refresh token exchange | Bearer token |
| [Smithery Stateless](#5-smithery-stateless) | Session parameters | Not supported | N/A |

## Communication Patterns

This document covers three distinct communication patterns:

1. **MCP Client → MCP Server → Nextcloud**: Interactive tool calls initiated by users through MCP clients (Claude Desktop, etc.)
2. **MCP Server → Nextcloud**: Background operations like vector sync that run without user interaction
3. **Astrolabe → MCP Server**: Nextcloud app backend communication for settings UI and unified search

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

**Implementation:** `context.py:78-79` - Returns shared client from lifespan context

#### Background Sync

Uses the same embedded credentials as interactive requests. The background job accesses Nextcloud with the configured username/password.

**Implementation:** Background jobs use `get_settings()` to access credentials

#### Astrolabe Integration

Not applicable - Astrolabe is only used in multi-user deployments where users need personal settings and token management.

---

### 2. Multi-User BasicAuth

**Use Case:** Internal deployment where users provide their own credentials via HTTP headers.

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
- `BasicAuthMiddleware` extracts credentials from `Authorization: Basic` header
- Credentials passed through to Nextcloud (not stored)
- Client created per-request from extracted credentials
- Stateless - no credential storage between requests

**Implementation:** `context.py:187-248` - `_get_client_from_basic_auth()` extracts credentials from request state

#### Background Sync (Optional)

Requires `ENABLE_OFFLINE_ACCESS=true`. Users can store app passwords via Astrolabe for background operations.

```
Astrolabe                     MCP Server                   Nextcloud
    │                             │                            │
    │── Store App Password ──────▶│                            │
    │   (via management API)      │                            │
    │                             │── Store in SQLite ────────▶│
    │                             │   (encrypted)              │
    │◀── Confirmation ────────────│                            │
    │                             │                            │
    │         [Background Job]    │                            │
    │                             │── Retrieve app password ──▶│
    │                             │   (from encrypted storage) │
    │                             │── HTTP + BasicAuth ───────▶│
    │                             │   (stored app password)    │
    │                             │◀── API Response ───────────│
```

**Requirements:**
- `ENABLE_OFFLINE_ACCESS=true`
- `TOKEN_ENCRYPTION_KEY` for credential encryption
- `TOKEN_STORAGE_DB` for SQLite storage path

#### Astrolabe → MCP Server

```
Astrolabe                     MCP Server                   Nextcloud OIDC
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
- Astrolabe has its own OAuth client (`astrolabe_client_id` in Nextcloud config)
- Tokens are validated by MCP server using Nextcloud OIDC JWKS
- Authorization check: `token.sub == requested_resource_owner`
- Any valid Nextcloud OIDC token accepted (relaxed audience validation per ADR-018)

**Implementation:** `unified_verifier.py:120-183` - `verify_token_for_management_api()` validates without strict audience check

---

### 3. OAuth Single-Audience (Default)

**Use Case:** Multi-user deployment with OAuth authentication. Tokens work for both MCP and Nextcloud.

This is the default mode when `NEXTCLOUD_USERNAME`/`NEXTCLOUD_PASSWORD` are not set.

#### MCP Client → MCP Server → Nextcloud

```
MCP Client                    MCP Server                   Nextcloud
    │                             │                            │
    │── Bearer Token ────────────▶│                            │
    │   aud: ["mcp-server",       │                            │
    │         "nextcloud"]        │                            │
    │                             │── Validate MCP audience ──▶│
    │                             │   (UnifiedTokenVerifier)   │
    │                             │                            │
    │                             │── HTTP + Same Token ──────▶│
    │                             │   Authorization: Bearer    │
    │                             │   (multi-audience token)   │
    │                             │                            │
    │                             │   NC validates its own aud │
    │                             │◀── API Response ───────────│
    │◀── Tool Result ─────────────│                            │
```

**Key characteristics:**
- Token contains both audiences: `aud: ["mcp-server", "nextcloud"]`
- MCP server validates only MCP audience (per RFC 7519)
- Nextcloud independently validates its own audience
- No token exchange needed - same token used throughout
- Stateless operation for interactive requests

**Token validation flow:**
1. `UnifiedTokenVerifier.verify_token()` validates MCP audience
2. Token passed directly to Nextcloud via `get_client_from_context()`
3. Nextcloud validates its own audience when receiving API calls

**Implementation:**
- `unified_verifier.py:185-252` - `_verify_mcp_audience()` validates MCP audience only
- `context.py:96-99` - Uses token directly in multi-audience mode

#### Background Sync

Requires `ENABLE_OFFLINE_ACCESS=true`. Uses stored refresh tokens to obtain access tokens for background operations.

```
                              MCP Server                   Nextcloud OIDC
                                  │                            │
    [Background Job starts]       │                            │
                                  │── Get refresh token ──────▶│
                                  │   (from encrypted storage) │
                                  │                            │
                                  │── Token refresh request ──▶│
                                  │   grant_type=refresh_token │
                                  │   scope=openid profile ... │
                                  │◀── New access + refresh ───│
                                  │   (rotation)               │
                                  │                            │
                                  │── Store rotated refresh ──▶│
                                  │   (encrypted)              │
                                  │                            │
                                  │── HTTP + Access Token ────▶│
                                  │   Authorization: Bearer    │
                                  │◀── API Response ───────────│
```

**Key characteristics:**
- Refresh tokens stored encrypted in SQLite (`TOKEN_STORAGE_DB`)
- Nextcloud OIDC rotates refresh tokens on every use (one-time use)
- `TokenBrokerService` handles token lifecycle
- Per-user locking prevents race conditions during concurrent refresh

**Implementation:**
- `token_broker.py:269-362` - `get_background_token()` handles refresh with locking
- `token_broker.py:428-509` - `_refresh_access_token_with_scopes()` exchanges refresh token

#### Astrolabe → MCP Server

Same as Multi-User BasicAuth. See [Astrolabe → MCP Server](#astrolabe--mcp-server) above.

---

### 4. OAuth Token Exchange (RFC 8693)

**Use Case:** Multi-user deployment where MCP tokens are separate from Nextcloud tokens. Provides stronger security boundaries.

Enabled by `ENABLE_TOKEN_EXCHANGE=true`.

#### MCP Client → MCP Server → Nextcloud

```
MCP Client                    MCP Server                   Nextcloud OIDC
    │                             │                            │
    │── Bearer Token ────────────▶│                            │
    │   aud: "mcp-server"         │                            │
    │   (MCP audience only)       │                            │
    │                             │── Validate MCP audience ──▶│
    │                             │                            │
    │                             │── RFC 8693 Exchange ──────▶│
    │                             │   grant_type=              │
    │                             │     urn:ietf:params:oauth: │
    │                             │     grant-type:token-exchange
    │                             │   subject_token=<mcp-token>│
    │                             │   requested_audience=      │
    │                             │     "nextcloud"            │
    │                             │◀── Delegated Token ────────│
    │                             │   aud: "nextcloud"         │
    │                             │                            │
    │                             │── HTTP + Delegated Token ─▶│
    │                             │   Authorization: Bearer    │
    │                             │◀── API Response ───────────│
    │◀── Tool Result ─────────────│                            │
```

**Key characteristics:**
- Strict audience separation: MCP token has `aud: "mcp-server"` only
- Server exchanges for Nextcloud-audience token on each request
- Ephemeral delegated tokens (not cached by default)
- Strongest security boundary between MCP and Nextcloud access

**Token exchange details:**
- Uses RFC 8693 "urn:ietf:params:oauth:grant-type:token-exchange"
- Subject token: MCP access token
- Requested audience: Nextcloud resource URI
- Result: Short-lived token scoped for Nextcloud

**Implementation:**
- `token_broker.py:220-267` - `get_session_token()` performs on-demand exchange
- `token_exchange.py` - `exchange_token_for_delegation()` implements RFC 8693
- `context.py:88-94` - Routes to session client in exchange mode

#### Background Sync

Same as OAuth Single-Audience. Uses stored refresh tokens from Flow 2 provisioning.

```
                              MCP Server                   Nextcloud OIDC
                                  │                            │
    [User provisions access]      │                            │
                                  │── Flow 2 OAuth ───────────▶│
                                  │   client_id="mcp-server"   │
                                  │   scope=offline_access ... │
                                  │◀── Refresh Token ──────────│
                                  │   (stored encrypted)       │
                                  │                            │
    [Background Job runs later]   │                            │
                                  │── Refresh for background ─▶│
                                  │   (same as single-audience)│
```

**Key difference from interactive:**
- Interactive: On-demand token exchange per request
- Background: Uses pre-provisioned refresh tokens (Flow 2)

#### Astrolabe → MCP Server

Same as Multi-User BasicAuth. See [Astrolabe → MCP Server](#astrolabe--mcp-server) above.

---

### 5. Smithery Stateless

**Use Case:** Multi-tenant SaaS deployment via Smithery platform. Fully stateless.

Enabled by `SMITHERY_DEPLOYMENT=true`.

#### MCP Client → MCP Server → Nextcloud

```
MCP Client                    MCP Server                   Nextcloud
    │                             │                            │
    │── SSE Connect ─────────────▶│                            │
    │   ?nextcloud_url=...        │                            │
    │   &username=...             │                            │
    │   &app_password=...         │                            │
    │                             │── SmitheryConfigMiddleware │
    │                             │   Extract URL params       │
    │                             │                            │
    │── MCP Request ─────────────▶│                            │
    │   (no Authorization header) │                            │
    │                             │── Create per-request ─────▶│
    │                             │   NextcloudClient          │
    │                             │                            │
    │                             │── HTTP + BasicAuth ───────▶│
    │                             │   (from session params)    │
    │                             │◀── API Response ───────────│
    │◀── Tool Result ─────────────│                            │
```

**Key characteristics:**
- Configuration passed via URL query parameters (Smithery `configSchema`)
- No persistent state - client created fresh per request
- No OAuth infrastructure
- No background sync support (stateless)
- No admin UI available

**Required session parameters:**
- `nextcloud_url`: Nextcloud instance URL
- `username`: Nextcloud username
- `app_password`: Nextcloud app password

**Implementation:** `context.py:108-184` - `_get_client_from_session_config()` creates client from session params

#### Background Sync

Not supported. Smithery mode is fully stateless with no credential storage.

#### Astrolabe Integration

Not applicable. Smithery deployments don't integrate with Astrolabe.

---

## Configuration Quick Reference

### Single-User BasicAuth
```bash
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password
```

### Multi-User BasicAuth
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
ENABLE_MULTI_USER_BASIC_AUTH=true

# Optional: For background sync
ENABLE_OFFLINE_ACCESS=true
TOKEN_ENCRYPTION_KEY=<32-byte-key>
TOKEN_STORAGE_DB=/data/tokens.db
```

### OAuth Single-Audience (Default)
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
# No username/password triggers OAuth mode

# Optional: Static client credentials (instead of DCR)
NEXTCLOUD_OIDC_CLIENT_ID=<client-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<client-secret>

# Optional: For background sync
ENABLE_OFFLINE_ACCESS=true
TOKEN_ENCRYPTION_KEY=<32-byte-key>
TOKEN_STORAGE_DB=/data/tokens.db
```

### OAuth Token Exchange
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
ENABLE_TOKEN_EXCHANGE=true
NEXTCLOUD_OIDC_CLIENT_ID=<client-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<client-secret>

# Optional: For background sync
ENABLE_OFFLINE_ACCESS=true
TOKEN_ENCRYPTION_KEY=<32-byte-key>
TOKEN_STORAGE_DB=/data/tokens.db
```

### Smithery Stateless
```bash
SMITHERY_DEPLOYMENT=true
# All other config comes from session URL parameters
```

---

## Related Documentation

- [Authentication](authentication.md) - Configuration details and setup guides
- [OAuth Architecture](oauth-architecture.md) - Deep OAuth protocol details
- [ADR-004: Progressive Consent](ADR-004-mcp-application-oauth.md) - Dual OAuth flow architecture
- [ADR-005: Token Audience Validation](ADR-005-token-audience-validation.md) - Audience validation strategy
- [ADR-018: Nextcloud PHP App](ADR-018-nextcloud-php-app-for-settings-ui.md) - Astrolabe integration
- [ADR-020: Deployment Modes](ADR-020-deployment-modes-and-configuration-validation.md) - Mode detection and validation
