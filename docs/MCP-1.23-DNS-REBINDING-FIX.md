# MCP 1.23.x DNS Rebinding Protection Fix

## Problem

MCP Python SDK 1.23.0 introduced **automatic DNS rebinding protection** that breaks containerized deployments (Kubernetes, Docker) when the protection is unintentionally auto-enabled.

### Root Cause

From `mcp/server/fastmcp/server.py:177-183` in the Python SDK:

```python
# Auto-enable DNS rebinding protection for localhost (IPv4 and IPv6)
if transport_security is None and host in ("127.0.0.1", "localhost", "::1"):
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
```

### What Was Happening

1. **FastMCP initialization** in `app.py` didn't pass `host` or `transport_security` parameters
2. **Defaults applied**: `host="127.0.0.1"`, `transport_security=None`
3. **Auto-enablement triggered**: Condition `transport_security is None and host == "127.0.0.1"` was TRUE
4. **Protection activated** with `allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`
5. **Kubernetes requests rejected**: `Host: nextcloud-mcp-server.default.svc.cluster.local:8000` didn't match allowed hosts

### Why `--host 0.0.0.0` Didn't Help

The `--host` CLI flag (used in Dockerfile/docker-compose) controls **uvicorn's bind address**, NOT the **FastMCP `host` parameter**. These are separate concerns:

- **Uvicorn bind address** (`--host 0.0.0.0`): Where the HTTP server listens
- **FastMCP host parameter** (defaulted to `"127.0.0.1"`): Used for auto-enablement logic

## Solution

Explicitly disable DNS rebinding protection by passing `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)` to all FastMCP instances.

### Changes Made

Modified `nextcloud_mcp_server/app.py`:

1. **Import** `TransportSecuritySettings` from `mcp.server.transport_security`
2. **Updated all three FastMCP initializations**:
   - OAuth mode (line 1015)
   - Smithery stateless mode (line 1030)
   - BasicAuth mode (line 1040)

Each now includes:
```python
transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
```

## Impact

### ✅ What This Fixes

- **Kubernetes deployments**: Requests with k8s service DNS names now work
- **Docker deployments**: Port-mapped requests (localhost:8000 → container) now work
- **Reverse proxy deployments**: Proxied requests with various Host headers now work
- **Ingress controllers**: Requests via ingress hostnames now work

### 🔒 Security Considerations

DNS rebinding protection defends against attacks where:
1. Attacker controls a DNS domain (e.g., `evil.com`)
2. DNS initially resolves to attacker's IP
3. After victim's browser caches the origin, DNS changes to victim's localhost
4. Attacker's page can now make requests to victim's localhost services

**Why it's safe to disable for this deployment:**

1. **OAuth authentication required** in production deployments (ADR-002, ADR-004)
2. **Network-level isolation** in containerized environments (k8s network policies, Docker networks)
3. **MCP is server-to-server**, not exposed to browsers (no CORS concerns)
4. **Host header validation inappropriate** for multi-tenant k8s environments

If DNS rebinding protection is needed for specific deployments, it can be re-enabled with a custom allowed hosts list:

```python
transport_security=TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "nextcloud-mcp-server.default.svc.cluster.local:*",
        "mcp.example.com:*",
        # Add all your expected Host header values
    ]
)
```

## Testing

- ✅ Ruff linting passes
- ✅ Type checking passes (pre-existing warnings unrelated)
- ✅ Module imports successfully
- ✅ Compatible with MCP 1.23.x

## References

- [MCP Python SDK 1.23.0 Release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.23.0)
- Commit: `d3a1841` - "Auto-enable DNS rebinding protection for localhost servers"
- Issue #373 (original report of k8s breakage)
- PR #382 (MCP 1.23.x upgrade)
