# Login Flow v2 (Multi-User Mode)

This is the recommended multi-user deployment mode for the Nextcloud MCP Server. It works with **stock Nextcloud 16+** (no upstream patches) and is the mode used by hosted offerings like [Astrolabe Cloud](https://astrolabecloud.com).

For the design rationale, see [ADR-022](ADR-022-deployment-mode-consolidation.md). For other deployment modes, see [Authentication](authentication.md).

## How It Works

Two authentication legs, each with a different mechanism:

```
┌─────────────────┐    OAuth/OIDC    ┌──────────────────┐   App password    ┌─────────────────┐
│   MCP Client    │ ───────────────> │   MCP Server     │ ────────────────> │   Nextcloud     │
│ (Claude, etc.)  │  (per-app scopes)│  (OIDC RP of IdP,│   (Basic Auth)    │   (NC 16+)      │
│                 │                  │  OAuth facade,   │                   │                 │
│                 │                  │  app-pwd holder) │                   │                 │
└─────────────────┘                  └──────────────────┘                   └─────────────────┘
                                              ▲
                                              │ OIDC discovery + token validation
                                              ▼
                                     ┌─────────────────┐
                                     │ OIDC Provider   │
                                     │ (Nextcloud OIDC,│
                                     │  Keycloak,      │
                                     │  AWS Cognito,…) │
                                     └─────────────────┘
```

- **MCP client → MCP server**: OAuth 2.1 with PKCE. The MCP server is **not** a standalone OAuth issuer — it acts as an OIDC relying party of a configurable identity provider and exposes an OAuth facade in front of it. The IdP is selected by `OIDC_DISCOVERY_URL` (defaults to Nextcloud's built-in OIDC); Keycloak, AWS Cognito, and other OIDC-compliant IdPs are also supported. Tokens are signed by that IdP, validated by the MCP server against the IdP's JWKS, and carry per-app scopes (`notes.read`, `talk.read`, `files.write`, …) that gate which tools the user can call.
- **MCP server → IdP (auth leg)**: The MCP server registers itself with the IdP via static `NEXTCLOUD_OIDC_CLIENT_ID`/`SECRET` (preferred — these are generic OIDC client credentials despite the Nextcloud-flavored naming, and work with any OIDC provider) or RFC 7591 DCR (fallback). This relationship is used for OIDC discovery, JWKS retrieval, and token validation.
- **MCP server → Nextcloud (data leg)**: Per-user **app password** obtained via Nextcloud's native [Login Flow v2](https://docs.nextcloud.com/server/latest/developer_manual/client_apis/LoginFlow/index.html#login-flow-v2). Sent as HTTP Basic Auth. Login Flow v2 is always Nextcloud's protocol regardless of which IdP authenticated the MCP client.

App passwords appear in **Settings → Security → Devices & Sessions** in Nextcloud and can be revoked by the user at any time.

### Why not forward OAuth bearer tokens to Nextcloud?

Earlier deployment modes forwarded the client's OAuth bearer token directly to Nextcloud APIs. That required upstream patches to `user_oidc` (Bearer-token validation on non-OCS endpoints) which were never merged. Nextcloud also doesn't enforce OAuth scopes on its app APIs even when Bearer tokens are accepted, so the security guarantees were weaker than they appeared. App passwords are the simplest mechanism that works on every supported Nextcloud version and surfaces user-revocable credentials in the standard UI.

Scope enforcement happens at the MCP server layer (defense-in-depth). See [Scope Enforcement](#scope-enforcement) below.

## Setup

### Required Environment Variables

```bash
# Nextcloud connection (data leg — always Nextcloud, regardless of which IdP authenticates clients)
NEXTCLOUD_HOST=https://your.nextcloud.example.com

# IdP selection (auth leg). Defaults to NEXTCLOUD_HOST/.well-known/openid-configuration
# (i.e. Nextcloud's built-in OIDC). Override to point at Keycloak, AWS Cognito, etc.
# OIDC_DISCOVERY_URL=https://keycloak.example.com/realms/myrealm/.well-known/openid-configuration

# OIDC client credentials for the MCP server's relying-party relationship with the IdP.
# These are generic OIDC client credentials — they work with any OIDC provider, despite
# the Nextcloud-flavored env-var names. Register a static client in your IdP
# (Nextcloud admin → OpenID Connect provider, Keycloak realm → Clients, etc.) and set these.
#
# Strongly recommended — do NOT rely on the DCR fallback with Nextcloud's built-in
# `oidc` app: it deletes dynamically-registered clients after ~1h, which breaks the
# connection permanently (see Troubleshooting → "Access forbidden" below).
NEXTCLOUD_OIDC_CLIENT_ID=<your-client-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<your-client-secret>

# Select Login Flow v2 mode (per-user Nextcloud app-password provisioning for the data leg)
MCP_DEPLOYMENT_MODE=login_flow

# App-password storage (required for persistence across restarts)
TOKEN_STORAGE_DB=/app/data/tokens.db
TOKEN_ENCRYPTION_KEY=<your-encryption-key>   # see "Generating an encryption key" below

# Public URLs (for browser redirects)
NEXTCLOUD_MCP_SERVER_URL=https://mcp.example.com
NEXTCLOUD_PUBLIC_ISSUER_URL=https://your.nextcloud.example.com  # Public URL of Nextcloud
```

When using an external IdP (Keycloak, Cognito, etc.), see [Keycloak Multi-Client Token Validation](keycloak-multi-client-validation.md) for how Nextcloud's `user_oidc` app handles realm-level token validation if you also federate Nextcloud's own login through the same IdP.

### Default IdP setup (Nextcloud's built-in `oidc` app)

When `OIDC_DISCOVERY_URL` is unset, Nextcloud's own **OpenID Connect provider**
(`oidc`) app is the IdP. Register a **static** client for the MCP server there —
don't rely on Dynamic Client Registration, because the `oidc` app auto-deletes
DCR clients after ~1 hour (see [Troubleshooting](#access-forbidden-after-the-connection-worked-for-a-while)).

1. Install/enable the **OpenID Connect provider** (`oidc`) app.
2. Go to **Administration settings → OpenID Connect provider → Add client** and set:
   - **Redirect URI:** `https://<your-mcp-server>/oauth/callback`
   - **Flow / response type:** authorization **code**
   - **Type:** **confidential** (so it issues a client secret)
   - **Resource identifier:** `https://<your-mcp-server>/mcp` (so issued tokens carry the MCP server's audience; the verifier's `_has_mcp_audience` accepts both this `/mcp` form and the bare server URL)
   - **Scopes:** leave empty to allow all, or list the per-app scopes you want plus `openid profile email offline_access`
3. Copy the generated client ID and secret into `NEXTCLOUD_OIDC_CLIENT_ID` /
   `NEXTCLOUD_OIDC_CLIENT_SECRET`.

### External IdP setup (Authentik / Keycloak / Cognito)

When `OIDC_DISCOVERY_URL` points at a third-party IdP rather than Nextcloud's own
OIDC, three things have to line up — and most setup confusion (e.g.
[#752](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/752)) comes from
mismatches across them.

#### Nextcloud apps to install

| App | When to install | Notes |
|---|---|---|
| `user_oidc` | **Required** if your external IdP also issues identities used by Nextcloud (i.e. you want SSO into Nextcloud through the same IdP). | Validates incoming Bearer tokens at the **realm** level — see [keycloak-multi-client-validation.md](keycloak-multi-client-validation.md). |
| `oidc` (Nextcloud-as-IdP) | **Skip.** | Only relevant when Nextcloud itself is the IdP. With an external IdP, `OIDC_DISCOVERY_URL` already points elsewhere. |
| `astrolabe` | **Optional.** | Provides a per-user "Enable Semantic Search" settings page that triggers Login Flow v2 from the Nextcloud UI. Without it, users provision via the `nc_auth_provision_access` MCP tool (which uses MCP elicitation for clients that support it). |

#### OIDC clients to register in your IdP

| Client | Required? | What it represents |
|---|---|---|
| **MCP server** | Yes | The MCP server's RP relationship with the IdP. Configured via `NEXTCLOUD_OIDC_CLIENT_ID` / `NEXTCLOUD_OIDC_CLIENT_SECRET`. Used for OIDC discovery, JWKS retrieval, and token validation. |
| **Astrolabe** | Only if Astrolabe is installed | Used by the Astrolabe Nextcloud app for its own per-user OAuth flow against the MCP server. |
| **MCP client** (e.g. Claude.ai, Claude Code) | Optional | The MCP server supports RFC 7591 Dynamic Client Registration, so MCP clients are auto-registered on first connect. Only register a static client if your IdP rejects DCR-issued clients or your MCP client cannot do DCR (see [#752 thread](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/752#issuecomment-4362197279) for the Claude Code workarounds). When pre-allowlisting static MCP clients, set `ALLOWED_MCP_CLIENTS` — see [`auth/client_registry.py`](../nextcloud_mcp_server/auth/client_registry.py) for the format. |

#### Scopes the IdP must advertise on the MCP-server client

Standard OIDC scopes (`openid`, `profile`, `email`) are not enough on their own —
the MCP server gates every Nextcloud-touching tool on a per-app scope (e.g.
`notes.read`, `calendar.write`). Those scopes must be issuable by the IdP on the
MCP-server client, otherwise the client's tokens won't carry them and tool calls
will be filtered out at `list_tools` time.

The authoritative list is served at:

```
GET https://<your-mcp-server>/.well-known/oauth-protected-resource/mcp
```

…in the `scopes_supported` field. In Authentik and Keycloak, register each scope
as a custom scope and expose it as a claim/scope mapping on the MCP-server
client. Add `offline_access` if you want refresh tokens for background sync.

If you also want resource-prefixed scopes (e.g. AWS Cognito's
`https://mcp.example.com/notes.read`), set `OIDC_RESOURCE_SERVER_ID` so the MCP
server strips the prefix before matching against `@require_scopes` decorators.
See `_strip_resource_prefix` in [`scope_authorization.py`](../nextcloud_mcp_server/auth/scope_authorization.py).

#### Diagnosing "OAuth succeeded but Nextcloud returns 401"

This is the most common failure mode after wiring up an external IdP, and it
trips up first-time setups. The two legs are independent:

```
MCP client ──── OAuth/OIDC ────> MCP server ──── Basic Auth ────> Nextcloud
            (auth leg, validated)             (data leg, app password)
```

If a tool call returns `401 Unauthorized` from a Nextcloud URL after OAuth
succeeded, the **data leg** has no credentials yet — the user hasn't completed
Login Flow v2 to provision an app password for that account. Fix it by calling
`nc_auth_provision_access` from the MCP client (or visiting the Astrolabe
settings page if installed). Subsequent tool calls reuse the stored app
password.

When the MCP client supports MCP elicitation (spec 2025-11-25), the server now
elicits a clickable Astrolabe settings URL automatically on the first failing
tool call, so the user has somewhere to click instead of just an error string.
Clients without elicitation support fall back to the existing
`ProvisioningRequiredError` text message.

### Generating an Encryption Key

App passwords are stored encrypted with Fernet. Generate a key once and reuse it:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Lose the key and stored app passwords become unrecoverable — users will need to re-provision.

### Docker Compose

The repo ships with a working reference under the `login-flow` profile:

```bash
docker compose --profile login-flow up -d
# Server listens on http://localhost:8004
```

Excerpt from `docker-compose.yml`:

```yaml
mcp-login-flow:
  build: .
  command: ["--transport", "streamable-http", "--oauth", "--port", "8004"]
  ports:
    - 127.0.0.1:8004:8004
  environment:
    - NEXTCLOUD_HOST=http://app:80
    - NEXTCLOUD_MCP_SERVER_URL=http://localhost:8004
    - NEXTCLOUD_PUBLIC_ISSUER_URL=http://localhost:8080
    - MCP_DEPLOYMENT_MODE=login_flow
    # Production: register a static OIDC client and set these — the DCR
    # fallback used by this dev/test service expires after ~1h against the
    # built-in `oidc` app (see "Default IdP setup" above and #907).
    # - NEXTCLOUD_OIDC_CLIENT_ID=<your-client-id>
    # - NEXTCLOUD_OIDC_CLIENT_SECRET=<your-client-secret>
    # Dev-only inline value. In production, mount via Docker secret and read
    # from a *_FILE env var or a secrets-management init step.
    - TOKEN_ENCRYPTION_KEY=<your-encryption-key>
    - TOKEN_STORAGE_DB=/app/data/tokens.db
  volumes:
    - login-flow-data:/app/data
    - login-flow-oauth-storage:/app/.oauth
```

> **Production note:** `TOKEN_ENCRYPTION_KEY` is a credential — losing it makes every stored app password unrecoverable. Inline-environment values are fine for local development but should be passed via Docker secrets (or your platform's equivalent) in production. See [Configuration → Best Practices for Docker](configuration.md#for-docker).

The `--oauth` flag enables the OAuth/OIDC identity layer that Login Flow v2 builds on (user identity via OAuth session, Nextcloud access via app passwords).

## Per-User Provisioning Flow

Each user goes through provisioning **once**, the first time they connect. Subsequent requests reuse the stored app password.

```
┌─────────────┐                  ┌──────────────────┐                  ┌─────────────────┐
│ MCP Client  │                  │   MCP Server     │                  │    Nextcloud    │
└──────┬──────┘                  └────────┬─────────┘                  └────────┬────────┘
       │  1. OAuth PKCE                   │                                     │
       ├─────────────────────────────────>│                                     │
       │  ← access token (per-app scopes) │                                     │
       │                                  │                                     │
       │  2. MCP request                  │                                     │
       ├─────────────────────────────────>│                                     │
       │                                  │                                     │
       │  3. No stored app password →     │                                     │
       │     elicit URL or 401            │                                     │
       │<─────────────────────────────────┤                                     │
       │  "Visit <login-url> to grant     │                                     │
       │   access"                        │                                     │
       │                                  │  4. POST /index.php/login/v2        │
       │                                  ├────────────────────────────────────>│
       │                                  │  ← {login_url, poll_endpoint, token}│
       │                                  │                                     │
       │  5. User opens login_url in browser, authenticates, clicks "Grant"     │
       │  ────────────────────────────────────────────────────────────────────> │
       │                                  │                                     │
       │                                  │  6. Poll endpoint (background)      │
       │                                  ├────────────────────────────────────>│
       │                                  │  ← {loginName, appPassword}         │
       │                                  │                                     │
       │                                  │  7. Encrypt + store in SQLite       │
       │                                  │                                     │
       │  8. Retry MCP request            │                                     │
       ├─────────────────────────────────>│                                     │
       │                                  │  9. GET /apps/notes/...             │
       │                                  ├────────────────────────────────────>│
       │                                  │  Authorization: Basic <app-pwd>     │
       │                                  │  ← response                         │
       │  10. ← result                    │                                     │
```

### Provisioning Endpoints

The server exposes browser endpoints for management UIs (Astrolabe, custom dashboards):

| Endpoint | Purpose |
|----------|---------|
| `GET /app/provision?redirect_uri=…` | Start Login Flow v2 and redirect to Nextcloud's grant page |
| `GET /app/provision/status?id=…` | Check whether the background poll has completed |

Both require a valid OAuth bearer token in the `Authorization` header (the user's identity is taken from the token, not from a query parameter).

Implementation: [`nextcloud_mcp_server/auth/provision_routes.py`](../nextcloud_mcp_server/auth/provision_routes.py).

### Provisioning via MCP Tools (Elicitation)

For MCP clients, the same flow is exposed as tools (`nc_auth_provision_access`, `nc_auth_check_status`). Clients that support **URL elicitation** (MCP spec 2025-11-25) get a clickable link automatically; clients without that capability fall back to a copy-paste URL in an error message. See [ADR-022 §"MCP Elicitation for Login Flow v2"](ADR-022-deployment-mode-consolidation.md) for the full capability matrix.

## Scope Enforcement

Nextcloud's app passwords have **no native scope support** — they grant the user's full API access. The MCP server enforces scopes at the application layer.

### Scope Reference

Scopes are **per-app** and follow an `<app>.<read|write>` pattern. There is no `mcp:` prefix.

| Scope | Covers |
|-------|--------|
| `notes.read` / `notes.write` | Notes app |
| `talk.read` / `talk.write` | Talk (spreed) |
| `files.read` / `files.write` | Files / WebDAV |
| `calendar.read` / `calendar.write` | Calendar (events + tasks/VTODO) |
| `contacts.read` / `contacts.write` | Contacts (CardDAV) |
| `deck.read` / `deck.write` | Deck |
| `tables.read` / `tables.write` | Tables |
| `cookbook.read` / `cookbook.write` | Cookbook |
| `todo.read` / `todo.write` | Tasks (VTODO outside Calendar) |
| `collectives.read` / `collectives.write` | Collectives |
| `news.read` | News (read-only) |
| `sharing.write` | Share-link / share-permission management |
| `semantic.read` | Semantic search + RAG (when enabled) |

The authoritative list is enumerated at runtime by [`scope_authorization.discover_all_scopes()`](../nextcloud_mcp_server/auth/scope_authorization.py) from each tool's `@require_scopes(...)` decorator and exposed via the PRM endpoint (`/.well-known/oauth-protected-resource/mcp`).

Standard OIDC scopes (`openid`, `profile`, `email`) are also accepted and have no effect on tool access.

### How Scopes Are Enforced

Each MCP tool is decorated with `@require_scopes(...)`:

```python
@mcp.tool()
@require_scopes("notes.read")
async def nc_notes_get_note(note_id: int, ctx: Context):
    ...
```

When a client calls `list_tools`, the server returns only tools the user has granted scopes for (dynamic tool filtering). When a client calls a tool whose scope is missing, the server returns:

```http
HTTP/1.1 403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope",
                  scope="notes.write",
                  resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
```

Clients can use this header to trigger **step-up authorization** — re-running the OAuth flow with additional scopes.

Implementation: [`nextcloud_mcp_server/auth/scope_authorization.py`](../nextcloud_mcp_server/auth/scope_authorization.py).

## OAuth Endpoints

When `--oauth` is enabled, the MCP server exposes OAuth 2.1 endpoints. **These endpoints front the configured IdP**: discovery metadata is sourced from the IdP, and tokens served via the MCP server's `/token` endpoint are signed by the IdP's key and validated against its JWKS — the MCP server has no signing keys of its own. The IdP is selected by `OIDC_DISCOVERY_URL` (Nextcloud OIDC by default, or Keycloak / Cognito / etc.).

| Endpoint | RFC | Purpose |
|----------|-----|---------|
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Server metadata (advertises the configured IdP as the upstream issuer) |
| `GET /.well-known/oauth-protected-resource/mcp` | RFC 9728 | PRM — advertises supported scopes (dynamically discovered from `@require_scopes`) |
| `POST /register` | RFC 7591 | Dynamic Client Registration (for MCP clients; see also `NEXTCLOUD_OIDC_CLIENT_ID/SECRET` for the MCP server's own RP credentials with the IdP) |
| `PUT/DELETE /register/{client_id}` | RFC 7592 | Client management with registration token |
| `GET /authorize` | RFC 6749 | Authorization endpoint (PKCE required, S256) |
| `POST /token` | RFC 6749 | Token endpoint |

Implementation: [`nextcloud_mcp_server/auth/oauth_routes.py`](../nextcloud_mcp_server/auth/oauth_routes.py), [`nextcloud_mcp_server/auth/client_registration.py`](../nextcloud_mcp_server/auth/client_registration.py).

PKCE with S256 is **mandatory** — required by the MCP specification and enforced at the authorization endpoint.

## Token Format

The MCP server can issue or accept either JWT or opaque access tokens depending on configuration.

| | JWT (recommended) | Opaque |
|---|---|---|
| Validation | Signature check via JWKS (local, fast) | Introspection HTTP call |
| Scope claim | Embedded in `scope` claim | Returned by introspection endpoint |
| Size | ~800-1200 chars | ~72 chars |
| Standard | RFC 9068 | RFC 7662 |

JWTs are preferred for production because validation is local and stateless. Opaque tokens are useful when you need server-side revocation without JWT blocklist infrastructure.

## Troubleshooting

### "Access forbidden" after the connection worked for a while

**Symptom:** authentication succeeds and tools work for a while (often up to an
hour), then the connection silently drops. Re-connecting redirects to Nextcloud
and shows an **"Access forbidden"** page. Restarting the MCP server and
re-creating the MCP client/connector don't help. ([#907](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/907))

**Cause:** you didn't set `NEXTCLOUD_OIDC_CLIENT_ID` / `NEXTCLOUD_OIDC_CLIENT_SECRET`,
so the MCP server registered *its own* relying-party client with Nextcloud's
built-in `oidc` app via Dynamic Client Registration (DCR). The `oidc` app treats
DCR clients as ephemeral and **deletes them after `client_expire_time` (default
3600s = 1 hour)** — it prunes expired DCR clients on every `/authorize` request.
Once the server's client is gone, `/authorize` can't find it (→ the "Access
forbidden" page) and token refresh fails too. It's permanent because the server
cached that now-deleted client in `tokens.db` and keeps reusing it.

**Fix:** register a **static** (admin-created, non-DCR) client and configure it —
see [Default IdP setup](#default-idp-setup-nextclouds-built-in-oidc-app). Static
clients are never auto-deleted. Set `NEXTCLOUD_OIDC_CLIENT_ID` /
`NEXTCLOUD_OIDC_CLIENT_SECRET` (they take precedence over the cached DCR client)
and recreate the container. Existing users will need to re-authorize once after
this switch — their stored sessions were issued to the now-deleted DCR client,
so old refresh tokens no longer validate against the new static client.

As a non-recommended stopgap you can extend the DCR client lifetime globally:
`occ config:app:set oidc client_expire_time --value 31536000`.

### "Provisioning loop" — user keeps being asked to authorize

Check that `TOKEN_STORAGE_DB` is on a persistent volume. The default (`/tmp` or per-process tempfile) is wiped on container restart, so each restart loses every stored app password.

### "Failed to start login flow" / 502 from `/app/provision`

The MCP server cannot reach Nextcloud at `NEXTCLOUD_HOST`. Verify network connectivity and that `NEXTCLOUD_HOST` uses an address reachable from the server (not the user's browser). For Docker Compose deployments, this is typically the internal service hostname (e.g. `http://app:80`).

### "Login URL points to localhost in browser"

`NEXTCLOUD_PUBLIC_ISSUER_URL` is missing or wrong. Set it to the public URL of Nextcloud as the user's browser sees it. The server rewrites the login URL's origin from the internal `NEXTCLOUD_HOST` to the browser-reachable Nextcloud URL before redirecting the browser.

### Login page 404s / lands on the IdP (external IdP, e.g. Keycloak)

With an **external** IdP, `NEXTCLOUD_PUBLIC_ISSUER_URL` points at the IdP (it doubles as the OAuth issuer for JWT validation), so the Login Flow v2 login URL gets rewritten onto the IdP's origin — which has no `/login/v2` endpoint and 404s. Set **`NEXTCLOUD_PUBLIC_URL`** to Nextcloud's own browser-reachable URL; it takes precedence over `NEXTCLOUD_PUBLIC_ISSUER_URL` for the login-page and elicitation-link rewrites while leaving JWT issuer validation on the IdP. Single-IdP (Nextcloud-is-the-IdP) deployments don't need it — the issuer URL already resolves to Nextcloud.

### Stored app password rejected by Nextcloud (401)

The user revoked it from **Settings → Security → Devices & Sessions**. Delete the row from the storage DB (or call `nc_auth_provision_access` again) to trigger a fresh Login Flow.

### `cryptography.fernet.InvalidToken` on startup

`TOKEN_ENCRYPTION_KEY` changed since the DB was created — stored app passwords cannot be decrypted with a different key. Either restore the original key or wipe the DB and have users re-provision.

### Multiple worker processes

The provisioning session store is in-memory; `MCP_DEPLOYMENT_MODE=login_flow` assumes a single worker. Running with `uvicorn --workers N` will cause provisioning sessions to randomly fail. For higher concurrency, scale horizontally (multiple containers behind a sticky-session load balancer) rather than within a single process.

> **Sticky-session keying:** route on the **user identity** (e.g. the `sub` claim from the OAuth Bearer token) — **not** the raw token value, and **not** source IP. Bearer tokens rotate on refresh, which would silently break token-value affinity if a refresh lands between the request that initiates provisioning and the polling request that completes it. MCP clients may also not maintain stable IPs across those requests. A stable per-user identifier extracted from the `Authorization` header (e.g. `sub`) is the right key.

## See Also

- [ADR-022: Deployment Mode Consolidation](ADR-022-deployment-mode-consolidation.md) — design rationale
- [Authentication](authentication.md) — overview of all deployment modes
- [Authentication Flows](auth-flows.md) — sequence diagrams per mode
- [Configuration](configuration.md) — full environment variable reference
- [Nextcloud Login Flow v2 spec](https://docs.nextcloud.com/server/latest/developer_manual/client_apis/LoginFlow/index.html#login-flow-v2)
