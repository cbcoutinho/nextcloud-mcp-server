"""End-to-end: an app password revoked in Nextcloud.

A user can revoke the app password the MCP server stores (Settings → Security →
Devices & sessions). Before this was handled, ``nc_auth_check_status`` kept
reporting ``provisioned`` and ``nc_auth_provision_access`` ``already_provisioned``
while every tool failed with 401, and no login URL was offered.

Against the live ``mcp-login-flow`` server (port 8004) and the real Nextcloud:
a fresh user provisions access, the app password is revoked with ``occ
user:auth-tokens:delete``, then

- ``nc_auth_check_status`` reports ``revoked`` and, being read-only, keeps doing
  so: it deletes nothing;
- ``nc_auth_provision_access`` removes the rejected password and starts a new
  Login Flow, after which the status is ``provisioned`` again with a new
  password in Nextcloud.
"""

import json
import logging
import subprocess
import uuid

import anyio
import pytest

from .conftest import _get_login_flow_token_for_user, _provision_login_flow_mcp_client

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]

#: ``IToken::PERMANENT_TOKEN``: app passwords, as Login Flow v2 creates them.
_PERMANENT_TOKEN = 1

#: Polls, 2 s apart, until Nextcloud's token cache has expired.
_REJECTION_ATTEMPTS = 45


def _occ(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "app", "php", "/var/www/html/occ", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"occ {' '.join(args)} failed: {result.stdout}{result.stderr}"
        )
    return result.stdout


def _app_password_ids(username: str) -> list[int]:
    tokens = json.loads(_occ("user:auth-tokens:list", username, "--output=json"))
    return [t["id"] for t in tokens if t["type"] == _PERMANENT_TOKEN]


def _revoke_app_passwords(username: str) -> None:
    ids = _app_password_ids(username)
    assert ids, f"{username} has no app password to revoke"
    for token_id in ids:
        _occ("user:auth-tokens:delete", username, str(token_id))


async def _call(session, tool: str, args: dict | None = None) -> dict:
    result = await session.call_tool(tool, args or {})
    return json.loads(result.content[0].text)


@pytest.fixture
async def revocable_user(anyio_backend, nc_client):
    """A fresh user, so revoking their app password disturbs no shared session."""
    suffix = uuid.uuid4().hex[:8]
    username = f"revoked_{suffix}"
    password = f"Revoked-{suffix}-Pass1!"
    await nc_client.users.create_user(userid=username, password=password)
    try:
        yield username, password
    finally:
        await nc_client.users.delete_user(username)


async def test_revoked_app_password_is_reported_then_replaced(
    browser, login_flow_oauth_client_credentials, oauth_callback_server, revocable_user
):
    username, password = revocable_user
    auth_states, _ = oauth_callback_server
    token = await _get_login_flow_token_for_user(
        browser, login_flow_oauth_client_credentials, auth_states, username, password
    )

    async for session in _provision_login_flow_mcp_client(
        token=token, browser=browser, username=username, password=password
    ):
        old_ids = _app_password_ids(username)
        _revoke_app_passwords(username)

        # occ runs in its own process and cannot clear the token cache of the
        # web server, so Nextcloud keeps accepting the password for a while
        # (measured on Nextcloud 32: still accepted after 5 s, rejected after 35 s).
        for _ in range(_REJECTION_ATTEMPTS):
            status = await _call(session, "nc_auth_check_status")
            if status["status"] == "revoked":
                break
            assert status["status"] == "provisioned", status
            await anyio.sleep(2)
        else:
            pytest.fail(f"Nextcloud kept accepting the revoked password: {status}")
        assert "nc_auth_provision_access" in status["message"]

        # Read-only: it deleted nothing, so a second call sees the same stored
        # password and reports the same.
        status = await _call(session, "nc_auth_check_status")
        assert status["status"] == "revoked", status

        # Provisioning removes the rejected password and starts a new flow;
        # the session's elicitation callback completes it in the browser.
        provision = await _call(session, "nc_auth_provision_access", {"scopes": None})
        assert provision["status"] != "already_provisioned", provision

        for _ in range(15):
            status = await _call(session, "nc_auth_check_status")
            if status["status"] == "provisioned":
                break
            assert status["status"] == "pending", status
            await anyio.sleep(2)
        else:
            pytest.fail(f"Login Flow did not complete after revocation: {status}")

        new_ids = _app_password_ids(username)
        assert new_ids, "no new app password in Nextcloud"
        assert not set(new_ids) & set(old_ids)
