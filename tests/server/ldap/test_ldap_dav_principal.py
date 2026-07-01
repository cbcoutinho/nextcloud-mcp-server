"""LDAP-backend reproduction of GH #980 (divergent loginName/UID DAV paths).

The LDAP user `alice` logs in as `alice` but Nextcloud's `user_ldap` backend
maps her to a canonical internal UID (the LDAP UUID). The multi-user BasicAuth
MCP server builds DAV paths from the loginName, so every WebDAV operation
targets ``/remote.php/dav/files/alice/`` — which does NOT resolve to her real
home at ``/remote.php/dav/files/<uid>/`` (unlike login-by-email, an LDAP login
is not a files-path alias, so it 404s rather than silently resolving).

* **Without** the #980 client fix → the round-trip targets the non-existent
  ``/files/alice/`` home and fails → the test below **fails** (RED). It is
  therefore marked ``xfail(strict=True)``: on ``master`` (no fix) it xfails, so
  CI stays green while still asserting the bug is present.
* **With** #980's ``BaseNextcloudClient._ensure_principal_id`` (a
  ``PROPFIND /remote.php/dav/`` for ``current-user-principal``) the real UID is
  discovered and the paths are rewritten → the round-trip succeeds → the test
  **xpasses**. ``strict=True`` then turns the unexpected pass into a CI failure,
  which is the signal to drop the ``xfail`` marker once #980 lands.

This is the live RED→GREEN reproduction that the Keycloak lane (PR #993) could
not provide, because email/`user_oidc` logins don't produce a non-resolvable
divergent path on the CI Nextcloud versions.
"""

import json

import pytest
from mcp import ClientSession

pytestmark = [pytest.mark.integration, pytest.mark.ldap]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Reproduces GH #980: DAV paths built from the LDAP loginName miss the "
        "canonical-UID home. Fixed by BaseNextcloudClient._ensure_principal_id "
        "(PR #980) — drop this marker once that lands."
    ),
)
async def test_webdav_round_trip_resolves_ldap_principal(
    nc_mcp_ldap_alice_client: ClientSession,
):
    """A full WebDAV cycle as the divergent LDAP user must hit her real home.

    create → write → read → list → delete, all as `alice`. Without #980 the
    paths are built from the loginName `alice` and the very first operation
    fails against the non-existent ``/files/alice/`` home.
    """
    dir_path = "/LdapPrincipalTest"
    file_path = f"{dir_path}/ldap_principal.txt"
    content = "webdav round-trip via the divergent LDAP principal"

    mkdir_result = await nc_mcp_ldap_alice_client.call_tool(
        "nc_webdav_create_directory", {"path": dir_path}
    )
    assert mkdir_result.isError is False, (
        "create_directory failed — DAV path built from the LDAP loginName "
        "'alice' instead of the discovered canonical UID (GH #980 not fixed?): "
        f"{mkdir_result.content}"
    )

    try:
        write_result = await nc_mcp_ldap_alice_client.call_tool(
            "nc_webdav_write_file",
            {"path": file_path, "content": content},
        )
        assert write_result.isError is False

        read_result = await nc_mcp_ldap_alice_client.call_tool(
            "nc_webdav_read_file", {"path": file_path}
        )
        assert read_result.isError is False
        read_data = json.loads(read_result.content[0].text)
        assert content in read_data.get("content", "")

        list_result = await nc_mcp_ldap_alice_client.call_tool(
            "nc_webdav_list_directory", {"path": dir_path}
        )
        assert list_result.isError is False
        list_data = json.loads(list_result.content[0].text)
        names = [f.get("name", "") for f in list_data.get("files", [])]
        assert "ldap_principal.txt" in names
    finally:
        await nc_mcp_ldap_alice_client.call_tool(
            "nc_webdav_delete_resource", {"path": file_path}
        )
        await nc_mcp_ldap_alice_client.call_tool(
            "nc_webdav_delete_resource", {"path": dir_path}
        )
