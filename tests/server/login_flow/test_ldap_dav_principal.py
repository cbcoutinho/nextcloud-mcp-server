"""Login-flow reproduction of GH #980 (divergent loginName/UID DAV paths).

This is the **login-flow** counterpart to
``tests/server/ldap/test_ldap_dav_principal.py`` (which drives the multi-user
BasicAuth service). The LDAP user `alice` logs in as `alice` but Nextcloud's
`user_ldap` backend maps her to a canonical internal UID (the LDAP UUID). In
login-flow mode identity comes via OIDC and API access via a per-user app
password, but DAV paths are still built from her loginName — so a naive WebDAV
operation targets ``/remote.php/dav/files/alice/``, which does NOT resolve to her
real home at ``/remote.php/dav/files/<uid>/`` (an LDAP login is not a files-path
alias, so it 404s rather than silently resolving).

``BaseNextcloudClient._ensure_principal_id`` issues a
``PROPFIND /remote.php/dav/`` for ``current-user-principal``, discovers the real
UID, and rewrites the base path. This test proves that fix holds in login-flow
mode too — the WebDAV round-trip below lands in alice's real home and **passes**.
"""

import json

import pytest
from mcp import ClientSession

pytestmark = [pytest.mark.integration, pytest.mark.login_flow_ldap]


async def test_webdav_round_trip_resolves_ldap_principal_login_flow(
    nc_mcp_login_flow_ldap_alice_client: ClientSession,
):
    """A full WebDAV cycle as the divergent LDAP user in login-flow mode.

    create → write → read → list → delete, all as `alice` provisioned via
    Login Flow v2. Principal discovery resolves the loginName `alice` to her
    canonical UID so every operation lands in her real home; before GH #980's fix
    the first operation failed against the non-existent ``/files/alice/`` home.
    """
    client = nc_mcp_login_flow_ldap_alice_client
    dir_path = "/LdapPrincipalTestLoginFlow"
    file_path = f"{dir_path}/ldap_principal.txt"
    content = "webdav round-trip via the divergent LDAP principal (login-flow)"

    mkdir_result = await client.call_tool(
        "nc_webdav_create_directory", {"path": dir_path}
    )
    assert mkdir_result.isError is False, (
        "create_directory failed — DAV path built from the LDAP loginName "
        "'alice' instead of the discovered canonical UID (GH #980 not fixed in "
        f"login-flow mode?): {mkdir_result.content}"
    )

    try:
        write_result = await client.call_tool(
            "nc_webdav_write_file",
            {"path": file_path, "content": content},
        )
        assert write_result.isError is False

        read_result = await client.call_tool("nc_webdav_read_file", {"path": file_path})
        assert read_result.isError is False
        read_data = json.loads(read_result.content[0].text)
        assert content in read_data.get("content", "")

        list_result = await client.call_tool(
            "nc_webdav_list_directory", {"path": dir_path}
        )
        assert list_result.isError is False
        list_data = json.loads(list_result.content[0].text)
        names = [f.get("name", "") for f in list_data.get("files", [])]
        assert "ldap_principal.txt" in names
    finally:
        await client.call_tool("nc_webdav_delete_resource", {"path": file_path})
        await client.call_tool("nc_webdav_delete_resource", {"path": dir_path})
