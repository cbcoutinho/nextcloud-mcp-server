"""Fixtures for the LDAP lane — reproduces GH #980 (divergent loginName/UID).

The `ldap` docker-compose profile runs an OpenLDAP server whose user `alice`
logs in as `alice` but is mapped by Nextcloud's `user_ldap` backend to a
canonical internal UID derived from the LDAP UUID (e.g.
`c2c0a34c-09dd-1041-...`). That divergence (`loginName != UID`) is the exact
shape of GH #980 that single-user, `user_oidc`, and login-by-email backends
cannot produce — and, unlike login-by-email, the LDAP login is NOT a valid
files-path alias, so `/remote.php/dav/files/alice/` genuinely misses the real
home at `/remote.php/dav/files/<uid>/`.

These tests drive the **multi-user BasicAuth** MCP service (port 8003): the
client sends `alice`'s LDAP credentials in the Authorization header, so the
server builds DAV paths from the loginName `alice`. Without the #980 client fix
(`BaseNextcloudClient._ensure_principal_id`) every DAV op targets the wrong,
non-existent home and fails; with it, current-user-principal discovery resolves
the real UID.
"""

import base64
from typing import Any, AsyncGenerator

import pytest
from mcp import ClientSession

from tests.conftest import create_mcp_client_session

# The multi-user BasicAuth MCP service (ADR-020) — builds the Nextcloud client
# per request from the BasicAuth username, i.e. the loginName `alice`.
MULTI_USER_BASIC_MCP_URL = "http://localhost:8003/mcp"

# Divergent LDAP user seeded by ldap/bootstrap.ldif.
LDAP_USERNAME = "alice"
LDAP_PASSWORD = "AlicePass123!"  # NOSONAR(S2068) - dev-only LDAP fixture credential


@pytest.fixture
async def nc_mcp_ldap_alice_client(
    anyio_backend,
) -> AsyncGenerator[ClientSession, Any]:
    """MCP session authenticated as the divergent LDAP user `alice` (port 8003).

    Connects to the multi-user BasicAuth service with `alice`'s LDAP credentials
    so the server constructs DAV paths from her loginName (`alice`), not her
    canonical UID — the condition GH #980 fixes.
    """
    credentials = base64.b64encode(f"{LDAP_USERNAME}:{LDAP_PASSWORD}".encode()).decode()
    async with create_mcp_client_session(
        url=MULTI_USER_BASIC_MCP_URL,
        headers={"Authorization": f"Basic {credentials}"},
        client_name="LDAP alice (multi-user-basic)",
    ) as session:
        yield session
