# Authentication

The Nextcloud MCP server authenticates to Nextcloud using **app-specific passwords** (HTTP Basic Auth). It supports three deployment modes that differ in how those credentials are sourced.

## Mode Comparison

| Mode | How credentials are obtained | Best for |
|------|------------------------------|----------|
| **Single-User (BasicAuth)** | App password in environment variables | Personal use, development, single-tenant deployments |
| **Multi-User (BasicAuth pass-through)** | MCP client sends credentials in HTTP `Authorization` header | Internal multi-user setups where users manage their own Nextcloud credentials |
| **Multi-User (Login Flow v2)** | Per-user app password obtained via Nextcloud's [Login Flow v2](https://docs.nextcloud.com/server/latest/developer_manual/client_apis/LoginFlow/index.html#login-flow-v2), stored encrypted | Hosted deployments, OAuth-based MCP clients (claude.ai, Astrolabe Cloud), production multi-user |

> **OAuth-direct-to-Nextcloud is no longer supported.** It required upstream patches to `user_oidc` that were never merged. Login Flow v2 replaces it for multi-user deployments and works with stock Nextcloud 16+. See [ADR-022](ADR-022-deployment-mode-consolidation.md) for the rationale.

## Single-User (BasicAuth)

One set of credentials is configured in the environment and shared by all MCP clients. All Nextcloud requests are made as the same user.

### Configuration

```bash
NEXTCLOUD_HOST=https://your.nextcloud.example.com
NEXTCLOUD_USERNAME=your_username
NEXTCLOUD_PASSWORD=your_app_password
```

Generate the app password in Nextcloud under **Settings → Security → Devices & sessions**. Don't use your login password.

### Trade-offs

- ✅ Simplest setup; no persistent state
- ✅ All MCP tools available (no scope enforcement — trusted environment)
- ❌ No per-user identity; all actions appear from the same account in Nextcloud
- ❌ Not suitable for shared deployments

See [Configuration](configuration.md) for the full environment variable reference.

## Multi-User (BasicAuth Pass-Through)

Each MCP client sends its own credentials in an HTTP `Authorization: Basic` header. The server creates a per-request Nextcloud client from those credentials and never persists them.

### Configuration

```bash
NEXTCLOUD_HOST=https://your.nextcloud.example.com
MCP_DEPLOYMENT_MODE=multi_user_basic
```

`NEXTCLOUD_USERNAME` and `NEXTCLOUD_PASSWORD` must NOT be set in this mode.

### Trade-offs

- ✅ Stateless — no token storage required
- ✅ Each user's actions are properly attributed in Nextcloud audit logs
- ❌ Clients must handle Nextcloud credentials directly (credential exposure risk)
- ❌ Not compatible with OAuth-based MCP clients (claude.ai, Astrolabe Cloud) without a credential bridge

## Multi-User (Login Flow v2)

The recommended mode for hosted and OAuth-based deployments. MCP clients authenticate to the MCP server via OAuth; the MCP server obtains a per-user app password from Nextcloud (via Login Flow v2) and uses HTTP Basic Auth to talk to Nextcloud APIs.

```
MCP Client ──(OAuth, per-app scopes)──> MCP Server ──(Basic Auth, app password)──> Nextcloud
```

The MCP server enforces per-app scopes (`notes.read`, `talk.write`, `files.read`, etc. — see [Login Flow v2 → Scope Reference](login-flow-v2.md#scope-reference)) at the application layer (defense-in-depth, since Nextcloud app passwords have no native scope support).

**See [Login Flow v2](login-flow-v2.md) for full setup, architecture, scope reference, and troubleshooting.**

## Mode Detection

The server detects the active mode from environment variables at startup:

| Env vars present | Detected mode |
|------------------|---------------|
| `NEXTCLOUD_USERNAME` + `NEXTCLOUD_PASSWORD` | Single-User (BasicAuth) |
| `MCP_DEPLOYMENT_MODE=multi_user_basic` | Multi-User (BasicAuth pass-through) |
| `MCP_DEPLOYMENT_MODE=login_flow` or no auth env vars set | Multi-User (Login Flow v2) |

You can also force a mode via CLI flag:

```bash
# Force Login Flow v2 / OAuth identity layer
uv run nextcloud-mcp-server --oauth

# Force BasicAuth
uv run nextcloud-mcp-server --no-oauth
```

## See Also

- [Login Flow v2](login-flow-v2.md) — multi-user setup details
- [Configuration](configuration.md) — environment variable reference
- [Authentication Flows](auth-flows.md) — sequence diagrams per mode
- [Running the Server](running.md) — start, manage, troubleshoot
- [Troubleshooting](troubleshooting.md) — common issues
- [ADR-022](ADR-022-deployment-mode-consolidation.md) — design rationale for mode consolidation
