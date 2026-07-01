"""End-to-end WebDAV coverage for the ``mcp-keycloak`` service (port 8002).

The keycloak lane previously only covered DCR / authorize — it never provisioned
a Login Flow v2 app password or exercised a real Nextcloud API call. These tests
close that gap: they drive a full Keycloak-fronted Login Flow v2 provisioning and
then run a WebDAV round-trip through the resulting app password.

``mcp-keycloak`` uses Keycloak as the external OAuth IdP but reaches Nextcloud via
Login Flow v2 app passwords (``MCP_DEPLOYMENT_MODE=login_flow``), exactly like
``mcp-login-flow``; only the OAuth IdP differs. Getting these tests to pass
requires the Login Flow v2 ``login_url`` to point the browser at *Nextcloud* — so
they are also the regression guard for ``NEXTCLOUD_PUBLIC_URL`` (in external-IdP
mode the OAuth issuer URL is Keycloak, not Nextcloud; without the dedicated
public-URL setting the login page 404s on Keycloak).

The fixtures log into Nextcloud Login Flow v2 as a *local* user via its **email**,
so the app password's stored ``loginName`` is the email while the canonical UID is
``divprincipal_<suffix>`` (loginName != UID). This exercises the same
identity-divergence shape as PR #980's client fix. Note: it does not reproduce
#980's wrong-path failure on the CI Nextcloud versions — Nextcloud resolves
``/remote.php/dav/files/<email>/`` to the user's real home, so the round-trip
succeeds regardless of the client-side principal-discovery fix. #980's failure
mode needs a backend (e.g. LDAP) where the loginName is not a valid files-path
alias; it is covered by #980's own mocked unit tests.
"""

import json
import logging

import pytest
from mcp import ClientSession

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.keycloak]


async def test_login_flow_stores_email_login_name(
    nc_mcp_keycloak_email_client: ClientSession,
    divergent_email_user: dict[str, str],
):
    """Login Flow v2 via email stores the email as the app password loginName.

    Confirms the Keycloak-fronted Login Flow v2 provisioning completed and that
    logging in by email yields a stored loginName that differs from the canonical
    UID (loginName != UID) — the identity shape PR #980 targets.
    """
    status_result = await nc_mcp_keycloak_email_client.call_tool(
        "nc_auth_check_status", {}
    )
    status_data = json.loads(status_result.content[0].text)

    assert status_data.get("status") == "provisioned"
    login_name = status_data.get("username")
    assert login_name == divergent_email_user["email"], (
        f"Expected loginName to be the email {divergent_email_user['email']!r}, "
        f"got {login_name!r}"
    )
    assert login_name != divergent_email_user["uid"], (
        "Expected loginName (email) to differ from the canonical UID."
    )


async def test_webdav_round_trip_via_keycloak_login_flow(
    nc_mcp_keycloak_email_client: ClientSession,
    divergent_email_user: dict[str, str],
):
    """A full WebDAV cycle works end-to-end through the keycloak service.

    Exercises create/write/read/list/delete against Nextcloud using the app
    password minted by Keycloak-fronted Login Flow v2. This is the keycloak-lane
    counterpart of the existing ``tests/server/login_flow`` WebDAV coverage, and
    it doubles as the regression guard for ``NEXTCLOUD_PUBLIC_URL`` — if the
    Login Flow v2 ``login_url`` were rewritten to the Keycloak origin again, the
    session fixture could not provision and this test would never run.
    """
    suffix = divergent_email_user["uid"].split("_")[-1]
    dir_path = f"/KeycloakLoginFlowTest_{suffix}"
    file_path = f"{dir_path}/keycloak_login_flow.txt"
    content = f"webdav round-trip via keycloak service {suffix}"

    mkdir_result = await nc_mcp_keycloak_email_client.call_tool(
        "nc_webdav_create_directory", {"path": dir_path}
    )
    assert mkdir_result.isError is False, (
        "create_directory failed — Keycloak Login Flow v2 WebDAV path is broken"
    )

    try:
        write_result = await nc_mcp_keycloak_email_client.call_tool(
            "nc_webdav_write_file",
            {"path": file_path, "content": content},
        )
        assert write_result.isError is False

        read_result = await nc_mcp_keycloak_email_client.call_tool(
            "nc_webdav_read_file", {"path": file_path}
        )
        assert read_result.isError is False
        read_data = json.loads(read_result.content[0].text)
        assert content in read_data.get("content", "")

        list_result = await nc_mcp_keycloak_email_client.call_tool(
            "nc_webdav_list_directory", {"path": dir_path}
        )
        assert list_result.isError is False
        list_data = json.loads(list_result.content[0].text)
        names = [f.get("name", "") for f in list_data.get("files", [])]
        assert "keycloak_login_flow.txt" in names
    finally:
        await nc_mcp_keycloak_email_client.call_tool(
            "nc_webdav_delete_resource", {"path": file_path}
        )
        await nc_mcp_keycloak_email_client.call_tool(
            "nc_webdav_delete_resource", {"path": dir_path}
        )
