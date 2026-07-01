"""Keycloak-service integration test for DAV current-user-principal discovery.

Reproduces the divergent-principal bug fixed by PR #980 against the
``mcp-keycloak`` service (port 8002), through Login Flow v2 app-password auth.

The ``divergent_email_user`` logs into Nextcloud Login Flow v2 via its **email**,
so the app password's stored loginName is the email while the canonical UID is
``divprincipal_<suffix>``. ``context.py`` builds the client with
``username = loginName = <email>``, so every DAV path is
``/remote.php/dav/files/<email>/`` — which is NOT the user's real home dir
(``/remote.php/dav/files/<uid>/``).

* **Without PR #980** the WebDAV operations target ``/files/<email>/`` and fail
  (Nextcloud has no home dir for the email) -> this test is RED.
* **With PR #980** ``BaseNextcloudClient._ensure_principal_id()`` issues a
  ``PROPFIND /remote.php/dav/`` for ``current-user-principal``, discovers the
  real UID, and rewrites the base path to ``/files/<uid>/`` -> GREEN.

This is the keycloak-service counterpart of the existing
``tests/server/login_flow`` WebDAV test, which was missing for the keycloak lane.
"""

import json
import logging

import pytest
from mcp import ClientSession

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.keycloak]


async def test_divergence_condition_holds(
    nc_mcp_keycloak_email_client: ClientSession,
    divergent_email_user: dict[str, str],
):
    """Guard: the provisioned app password's loginName is the email, not the UID.

    If this ever stops holding, the WebDAV test below would be a false pass
    (username == uid means paths are trivially correct even without the fix).
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
        "Divergence precondition failed: loginName == UID, so the wrong-path bug "
        "cannot be reproduced."
    )


async def test_webdav_operations_resolve_divergent_principal(
    nc_mcp_keycloak_email_client: ClientSession,
    divergent_email_user: dict[str, str],
):
    """Full WebDAV cycle succeeds only when the real UID home dir is resolved.

    Without PR #980 the paths are built from the email loginName and every
    operation targets a non-existent home dir -> failures (RED). With the fix,
    current-user-principal discovery resolves the UID -> success (GREEN).
    """
    suffix = divergent_email_user["uid"].split("_")[-1]
    dir_path = f"/KeycloakPrincipalTest_{suffix}"
    file_path = f"{dir_path}/divergent_principal.txt"
    content = f"principal discovery via keycloak service {suffix}"

    mkdir_result = await nc_mcp_keycloak_email_client.call_tool(
        "nc_webdav_create_directory", {"path": dir_path}
    )
    assert mkdir_result.isError is False, (
        "create_directory failed — DAV path likely built from the email "
        "loginName instead of the discovered UID (PR #980 not applied?)"
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
        assert "divergent_principal.txt" in names
    finally:
        await nc_mcp_keycloak_email_client.call_tool(
            "nc_webdav_delete_resource", {"path": file_path}
        )
        await nc_mcp_keycloak_email_client.call_tool(
            "nc_webdav_delete_resource", {"path": dir_path}
        )
