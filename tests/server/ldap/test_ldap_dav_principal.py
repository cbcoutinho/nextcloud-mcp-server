"""LDAP-backend reproduction of GH #980 (divergent loginName/UID DAV paths).

The LDAP user `alice` logs in as `alice` but Nextcloud's `user_ldap` backend
maps her to a canonical internal UID (the LDAP UUID). The multi-user BasicAuth
MCP server would build DAV paths from the loginName, so a naive WebDAV operation
targets ``/remote.php/dav/files/alice/`` — which does NOT resolve to her real
home at ``/remote.php/dav/files/<uid>/`` (unlike login-by-email, an LDAP login
is not a files-path alias, so it 404s rather than silently resolving).

This is the regression guard for that bug: ``BaseNextcloudClient._ensure_principal_id``
issues a ``PROPFIND /remote.php/dav/`` for ``current-user-principal``, discovers
the real UID, and rewrites the base path — so the WebDAV round-trip below lands
in alice's real home and **passes**. Before that fix (GH #980) it failed: the
round-trip targeted the non-existent ``/files/alice/`` home.

It is the live reproduction that the Keycloak lane (PR #993) could not provide,
because email/`user_oidc` logins don't produce a non-resolvable divergent path
on the CI Nextcloud versions.
"""

import json

import pytest
from mcp import ClientSession

pytestmark = [pytest.mark.integration, pytest.mark.ldap]


async def test_webdav_round_trip_resolves_ldap_principal(
    nc_mcp_ldap_alice_client: ClientSession,
):
    """A full WebDAV cycle as the divergent LDAP user must hit her real home.

    create → write → read → list → delete, all as `alice`. Principal discovery
    resolves the loginName `alice` to her canonical UID so every operation lands
    in her real home; before GH #980's fix the first operation failed against
    the non-existent ``/files/alice/`` home.
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
