"""
End-to-end tests for the oidc app's per-group scope limits (Deck card #1329).

A group with a limit caps the scopes its members can be issued, for every
client (including DCR clients) and on every issuance path. Two paths are
exercised against the real stack:

- the authorization-code flow of a DCR client (the JWT ``scope`` claim), and
- Astrolabe's ``TokenGenerationRequestEvent`` minting, observed through
  ``occ astrolabe:mcp-probe`` as the MCP tools the minted token unlocks.

The limits are managed with ``occ oidc:group-scopes:*``; the module skips when
the installed oidc app predates that command (app-store release without the
feature).
"""

import logging
import subprocess
import uuid

import pytest

from ...conftest import _get_oauth_token_with_scopes
from .test_dcr_token_type import decode_jwt_payload

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]

DEFAULT_SCOPE = {"openid", "profile", "email", "roles"}
REQUESTED = "openid profile email notes.read notes.write files.read"


def _occ(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "app", "php", "/var/www/html/occ", *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"occ {' '.join(args)} failed: {result.stdout}{result.stderr}"
        )
    return result


@pytest.fixture(scope="module")
def group_scopes_supported():
    if _occ("oidc:group-scopes:list", check=False).returncode != 0:
        pytest.skip(
            "Installed oidc app has no group scope limits (oidc:group-scopes:*)"
        )


@pytest.fixture
async def limited_user(anyio_backend, nc_client, group_scopes_supported):
    """A fresh user plus a factory that puts them in a group with a limit."""
    suffix = uuid.uuid4().hex[:8]
    username = f"scopeceil_{suffix}"
    password = f"ScopeCeil-{suffix}-Pass1!"
    await nc_client.users.create_user(userid=username, password=password)
    groups: list[str] = []

    async def limit(ceiling: str) -> str:
        gid = f"scopeceil_{suffix}_{len(groups)}"
        await nc_client.groups.create_group(gid)
        groups.append(gid)
        await nc_client.users.add_user_to_group(username, gid)
        _occ("oidc:group-scopes:set", gid, ceiling)
        return gid

    try:
        yield username, password, limit
    finally:
        for gid in groups:
            _occ("oidc:group-scopes:delete", gid, check=False)
            await nc_client.groups.delete_group(gid)
        await nc_client.users.delete_user(username)


async def _token_scopes(
    browser, client, callback_server, username, password
) -> set[str]:
    token = await _get_oauth_token_with_scopes(
        browser,
        client,
        callback_server,
        scopes=REQUESTED,
        username=username,
        password=password,
    )
    return set(decode_jwt_payload(token)["scope"].split())


async def test_authorize_clamps_scope_to_group_limit(
    browser, shared_jwt_oauth_client_credentials, oauth_callback_server, limited_user
):
    username, password, limit = limited_user
    await limit("notes.read")

    scopes = await _token_scopes(
        browser,
        shared_jwt_oauth_client_credentials,
        oauth_callback_server,
        username,
        password,
    )

    assert "notes.read" in scopes
    assert scopes <= DEFAULT_SCOPE | {"notes.read"}, scopes


async def test_group_limits_are_unioned(
    browser, shared_jwt_oauth_client_credentials, oauth_callback_server, limited_user
):
    username, password, limit = limited_user
    await limit("notes.read")
    await limit("files.read")

    scopes = await _token_scopes(
        browser,
        shared_jwt_oauth_client_credentials,
        oauth_callback_server,
        username,
        password,
    )

    assert {"notes.read", "files.read"} <= scopes
    assert "notes.write" not in scopes, scopes


def _probe_tools(user: str) -> set[str]:
    """Tool names visible to ``user`` through Astrolabe's minted token."""
    out = _occ("astrolabe:mcp-probe", user, "--scopes", "notes.read files.read").stdout
    return {line.split()[0] for line in out.splitlines() if line.startswith("  nc_")}


async def test_astrolabe_minted_token_respects_group_limit(limited_user):
    """TokenGenerationRequestEvent (Astrolabe's path) applies the same limit."""
    username, _, limit = limited_user
    await limit("files.read")

    tools = _probe_tools(username)

    assert any(t.startswith("nc_webdav_") for t in tools), tools
    assert not any(t.startswith("nc_notes_") for t in tools), tools
    # Control: an unlimited user gets notes tools for the same request.
    assert any(t.startswith("nc_notes_") for t in _probe_tools("admin"))
