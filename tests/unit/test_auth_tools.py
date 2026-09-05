"""Unit tests for Login Flow v2 MCP auth tools.

Tests the auth tools logic with mocked storage and Login Flow client.
"""

import secrets
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.auth.login_flow import LoginFlowPollResult
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES
from nextcloud_mcp_server.server.auth_tools import register_auth_tools

pytestmark = pytest.mark.unit


def _capture_registered_tools() -> dict:
    """Register the auth tools against a stub MCP and return them by name.

    ``register_auth_tools`` only uses ``@mcp.tool(...)`` decorators, so a stub
    whose ``tool()`` returns an identity decorator captures the closures without
    a real MCPServer instance.
    """
    captured: dict = {}

    class _StubMCP:
        def tool(self, *args, **kwargs):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    register_auth_tools(cast(MCPServer, _StubMCP()))
    return captured


@pytest.fixture
def encryption_key():
    """Generate a test encryption key."""
    return Fernet.generate_key().decode()


@pytest.fixture
async def temp_storage(encryption_key):
    """Create temporary storage with encryption for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_auth_tools.db"
        storage = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=encryption_key
        )
        await storage.initialize()
        yield storage


async def test_store_app_password_with_scopes(temp_storage):
    """Test storing app password with scopes."""
    await temp_storage.store_app_password_with_scopes(
        user_id="alice",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
        scopes=["notes.read", "notes.write"],
        username="alice_nc",
    )

    data = await temp_storage.get_app_password_with_scopes("alice")
    assert data is not None
    assert data["app_password"] == "aaaaa-bbbbb-ccccc-ddddd-eeeee"
    assert data["scopes"] == ["notes.read", "notes.write"]
    assert data["username"] == "alice_nc"
    assert data["created_at"] is not None
    assert data["updated_at"] is not None


async def test_store_app_password_null_scopes(temp_storage):
    """Test storing app password with NULL scopes (all allowed)."""
    await temp_storage.store_app_password_with_scopes(
        user_id="bob",
        app_password="fffff-ggggg-hhhhh-iiiii-jjjjj",
        scopes=None,
    )

    data = await temp_storage.get_app_password_with_scopes("bob")
    assert data is not None
    assert data["scopes"] is None  # NULL = all scopes allowed
    assert data["username"] is None


async def test_store_app_password_with_scopes_replaces(temp_storage):
    """Test that storing replaces existing record."""
    await temp_storage.store_app_password_with_scopes(
        user_id="alice",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
        scopes=["notes.read"],
    )
    await temp_storage.store_app_password_with_scopes(
        user_id="alice",
        app_password="xxxxx-yyyyy-zzzzz-aaaaa-bbbbb",
        scopes=["notes.read", "calendar.read"],
        username="alice_nc",
    )

    data = await temp_storage.get_app_password_with_scopes("alice")
    assert data["app_password"] == "xxxxx-yyyyy-zzzzz-aaaaa-bbbbb"
    assert data["scopes"] == ["notes.read", "calendar.read"]


async def test_get_app_password_with_scopes_nonexistent(temp_storage):
    """Test getting scoped password for non-existent user."""
    data = await temp_storage.get_app_password_with_scopes("nonexistent")
    assert data is None


# ── Login Flow Session Tests ──


async def test_store_and_get_login_flow_session(temp_storage):
    """Test storing and retrieving a login flow session."""
    await temp_storage.store_login_flow_session(
        user_id="alice",
        poll_token="secret-poll-token",
        poll_endpoint="https://cloud.example.com/login/v2/poll",
        requested_scopes=["notes.read", "notes.write"],
    )

    session = await temp_storage.get_login_flow_session("alice")
    assert session is not None
    assert session["poll_token"] == "secret-poll-token"
    assert session["poll_endpoint"] == "https://cloud.example.com/login/v2/poll"
    assert session["requested_scopes"] == ["notes.read", "notes.write"]
    assert session["created_at"] is not None
    assert session["expires_at"] is not None


async def test_get_login_flow_session_nonexistent(temp_storage):
    """Test getting session for user with no pending flow."""
    session = await temp_storage.get_login_flow_session("nonexistent")
    assert session is None


async def test_get_login_flow_session_expired(temp_storage):
    """Test that expired sessions are not returned."""
    await temp_storage.store_login_flow_session(
        user_id="alice",
        poll_token="expired-token",
        poll_endpoint="https://cloud.example.com/login/v2/poll",
        expires_at=1,  # Expired long ago
    )

    session = await temp_storage.get_login_flow_session("alice")
    assert session is None


async def test_delete_login_flow_session(temp_storage):
    """Test deleting a login flow session."""
    await temp_storage.store_login_flow_session(
        user_id="alice",
        poll_token="token",
        poll_endpoint="https://cloud.example.com/poll",
    )

    deleted = await temp_storage.delete_login_flow_session("alice")
    assert deleted is True

    # Verify it's gone
    session = await temp_storage.get_login_flow_session("alice")
    assert session is None


async def test_delete_login_flow_session_nonexistent(temp_storage):
    """Test deleting a non-existent session returns False."""
    deleted = await temp_storage.delete_login_flow_session("nonexistent")
    assert deleted is False


async def test_delete_expired_login_flow_sessions(temp_storage):
    """Test cleanup of expired sessions."""
    # Store 2 expired and 1 valid session
    await temp_storage.store_login_flow_session(
        user_id="expired1",
        poll_token="t1",
        poll_endpoint="https://cloud.example.com/poll",
        expires_at=1,
    )
    await temp_storage.store_login_flow_session(
        user_id="expired2",
        poll_token="t2",
        poll_endpoint="https://cloud.example.com/poll",
        expires_at=2,
    )
    await temp_storage.store_login_flow_session(
        user_id="valid",
        poll_token="t3",
        poll_endpoint="https://cloud.example.com/poll",
        # Default expiry = 20 minutes from now
    )

    count = await temp_storage.delete_expired_login_flow_sessions()
    assert count == 2

    # Valid session should still exist
    session = await temp_storage.get_login_flow_session("valid")
    assert session is not None


# ── Response Model Tests ──


def test_all_supported_scopes():
    """Test that ALL_SUPPORTED_SCOPES contains expected scopes.

    Read/write pairing is deliberately not asserted: it is not a property of
    this set (mail.send pairs with nothing, News is read-only in practice), and
    the assertion that used to live here matched on ':read'/':write', which
    stopped matching anything when ADR-024 moved the separator to a dot — so it
    passed vacuously for months. Coverage of the invariant that actually
    matters lives in tests/unit/test_scope_vocabulary_drift.py.
    """
    assert "notes.read" in ALL_SUPPORTED_SCOPES
    assert "notes.write" in ALL_SUPPORTED_SCOPES
    assert "calendar.read" in ALL_SUPPORTED_SCOPES
    assert "files.read" in ALL_SUPPORTED_SCOPES
    assert "deck.read" in ALL_SUPPORTED_SCOPES
    assert "semantic.read" in ALL_SUPPORTED_SCOPES


# ── Provisioning defaults ──


async def test_provision_access_without_scopes_stores_no_restriction(mocker):
    """Omitting `scopes` must persist NULL, not a snapshot of the vocabulary.

    NULL is what the Nextcloud-side provisioning routes write, so both paths
    agree, and the OAuth token stays the single live source of truth. Storing a
    list here instead goes stale as soon as an admin edits the OIDC client —
    which is how semantic.read became permanently ungrantable (GH #1277).
    """
    provision = _capture_registered_tools()["nc_auth_provision_access"]

    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.extract_user_id_from_token",
        AsyncMock(return_value="alice"),
    )
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(return_value=None)
    storage.store_login_flow_session = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_shared_storage",
        AsyncMock(return_value=storage),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_settings",
        return_value=MagicMock(nextcloud_host="https://nc", nextcloud_browser_url=None),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_nextcloud_ssl_verify",
        return_value=False,
    )
    flow_client = AsyncMock()
    flow_client.initiate = AsyncMock(
        return_value=MagicMock(
            poll_token="tok",
            poll_endpoint="https://nc/login/v2/poll",
            login_url="https://nc/login/v2/flow",
        )
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.LoginFlowV2Client",
        return_value=flow_client,
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.elicitation.present_login_url",
        AsyncMock(return_value="message_only"),
    )

    response = await provision(MagicMock())

    assert response.requested_scopes is None
    assert (
        storage.store_login_flow_session.await_args.kwargs["requested_scopes"] is None
    )


async def test_provision_access_rejects_invalid_scopes(mocker):
    """An explicit list is still validated — and the None default must not make
    that comprehension blow up on a missing list."""
    provision = _capture_registered_tools()["nc_auth_provision_access"]

    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.extract_user_id_from_token",
        AsyncMock(return_value="alice"),
    )
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(return_value=None)
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_shared_storage",
        AsyncMock(return_value=storage),
    )

    response = await provision(MagicMock(), scopes=["notes.read", "not.a.scope"])

    assert response.success is False
    assert "not.a.scope" in response.message


async def test_provision_access_rejects_empty_scope_list(mocker):
    """`scopes=[]` must not be folded into "no restriction".

    Both are falsy but they ask for opposite things — "restrict me to nothing"
    versus "do not restrict me" — so treating them alike widens access instead
    of denying it.
    """
    provision = _capture_registered_tools()["nc_auth_provision_access"]

    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.extract_user_id_from_token",
        AsyncMock(return_value="alice"),
    )
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(return_value=None)
    storage.store_login_flow_session = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_shared_storage",
        AsyncMock(return_value=storage),
    )

    response = await provision(MagicMock(), scopes=[])

    assert response.success is False
    assert "empty scope list" in response.message
    storage.store_login_flow_session.assert_not_awaited()


# ── Updating scopes on an unrestricted (NULL) grant ──


def _update_scopes_storage(mocker, previous_scopes):
    """Wire nc_auth_update_scopes against a user with the given stored grant.

    A change (as opposed to a no-op) re-runs Login Flow v2, so the flow client
    is stubbed too — otherwise the tool reports a connection failure and the
    assertion under test never runs.
    """
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.extract_user_id_from_token",
        AsyncMock(return_value="alice"),
    )
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(
        return_value={"scopes": previous_scopes, "app_password": "x"}
    )
    storage.store_login_flow_session = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_shared_storage",
        AsyncMock(return_value=storage),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_settings",
        return_value=MagicMock(nextcloud_host="https://nc", nextcloud_browser_url=None),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_nextcloud_ssl_verify",
        return_value=False,
    )
    flow_client = AsyncMock()
    flow_client.initiate = AsyncMock(
        return_value=MagicMock(
            poll_token="tok",
            poll_endpoint="https://nc/login/v2/poll",
            login_url="https://nc/login/v2/flow",
        )
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.LoginFlowV2Client",
        return_value=flow_client,
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.elicitation.present_login_url",
        AsyncMock(return_value="message_only"),
    )
    return storage


async def test_update_scopes_add_on_unrestricted_grant_is_a_no_op(mocker):
    """Adding to a NULL grant changes nothing, and must say why.

    The stored layer already places no restriction, so the only thing that can
    still be denying the call is the OAuth token — which this tool cannot
    widen. Saying just "unchanged" sends an agent into a retry loop, since the
    denial that got it here named this very tool.
    """
    update = _capture_registered_tools()["nc_auth_update_scopes"]
    _update_scopes_storage(mocker, None)

    response = await update(MagicMock(), add_scopes=["semantic.read"])

    assert response.status == "unchanged"
    assert "OAuth token" in response.message


async def test_update_scopes_remove_on_unrestricted_grant_materialises_a_list(mocker):
    """Narrowing a NULL grant necessarily snapshots the vocabulary.

    Restrictions are stored as an allow-list, so "everything except X" can only
    be written as a concrete list — which then does not include scopes added to
    the vocabulary later, even when the token grants them. That is the same
    staleness this change removes elsewhere, accepted here rather than
    introducing a second (deny-list) representation for a rare, explicitly
    user-driven request. Locked in so the trade-off is visible if it ever stops
    being acceptable.
    """
    update = _capture_registered_tools()["nc_auth_update_scopes"]
    _update_scopes_storage(mocker, None)

    response = await update(MagicMock(), remove_scopes=["mail.send"])

    assert response.status != "unchanged"
    assert response.new_scopes is not None
    assert "mail.send" not in response.new_scopes
    assert set(response.new_scopes) == ALL_SUPPORTED_SCOPES - {"mail.send"}


# ── Background-sync wake on provisioning ──


async def test_check_status_completion_wakes_user_manager(mocker):
    """When nc_auth_check_status polls a completed Login Flow, it stores the app
    password and rings the background-sync doorbell (the server/auth_tools.py
    wake path)."""
    check_status = _capture_registered_tools()["nc_auth_check_status"]

    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.extract_user_id_from_token",
        AsyncMock(return_value="alice"),
    )
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(return_value=None)  # not yet
    storage.get_login_flow_session = AsyncMock(
        return_value={
            "poll_endpoint": "https://nc/login/v2/poll",
            "poll_token": "tok",
            "requested_scopes": None,
        }
    )
    storage.store_app_password_with_scopes = AsyncMock()
    storage.delete_login_flow_session = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_shared_storage",
        AsyncMock(return_value=storage),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_settings",
        return_value=MagicMock(
            nextcloud_host="https://nc", nextcloud_public_issuer_url=None
        ),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_nextcloud_ssl_verify",
        return_value=False,
    )
    mocker.patch("nextcloud_mcp_server.server.auth_tools.invalidate_scope_cache")

    flow_client = AsyncMock()
    flow_client.poll = AsyncMock(
        return_value=LoginFlowPollResult(
            status="completed",
            login_name="alice",
            app_password=secrets.token_urlsafe(24),  # generated, not a literal
        )
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.LoginFlowV2Client",
        return_value=flow_client,
    )
    notify = mocker.patch("nextcloud_mcp_server.app.notify_user_provisioned")

    response = await check_status(MagicMock())

    assert response.status == "provisioned"
    storage.store_app_password_with_scopes.assert_awaited_once()
    notify.assert_called_once()


async def test_check_status_polls_pending_flow_while_still_provisioned(mocker):
    """A scope update must complete even though the old app password is still stored.

    nc_auth_update_scopes deliberately leaves the previous password in place
    while the new Login Flow runs. Reporting that stored grant before polling
    stranded the update forever: the user approved files.write, but every
    nc_auth_check_status kept answering "provisioned, scopes=[files.read]"
    and the new password was never stored (GH #1431).
    """
    check_status = _capture_registered_tools()["nc_auth_check_status"]

    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.extract_user_id_from_token",
        AsyncMock(return_value="alice"),
    )
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(
        return_value={
            "scopes": ["files.read"],
            "app_password": "old",
            "username": "alice",
        }
    )
    storage.get_login_flow_session = AsyncMock(
        return_value={
            "poll_endpoint": "https://nc/login/v2/poll",
            "poll_token": "tok",
            "requested_scopes": ["files.read", "files.write"],
        }
    )
    storage.store_app_password_with_scopes = AsyncMock()
    storage.delete_login_flow_session = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_shared_storage",
        AsyncMock(return_value=storage),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_settings",
        return_value=MagicMock(
            nextcloud_host="https://nc", nextcloud_public_issuer_url=None
        ),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_nextcloud_ssl_verify",
        return_value=False,
    )
    mocker.patch("nextcloud_mcp_server.server.auth_tools.invalidate_scope_cache")

    flow_client = AsyncMock()
    flow_client.poll = AsyncMock(
        return_value=LoginFlowPollResult(
            status="completed",
            login_name="alice",
            app_password=secrets.token_urlsafe(24),
        )
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.LoginFlowV2Client",
        return_value=flow_client,
    )
    mocker.patch("nextcloud_mcp_server.app.notify_user_provisioned")

    response = await check_status(MagicMock())

    assert response.status == "provisioned"
    assert response.scopes == ["files.read", "files.write"]
    assert storage.store_app_password_with_scopes.await_args.kwargs["scopes"] == [
        "files.read",
        "files.write",
    ]


async def test_check_status_expired_flow_keeps_previous_grant(mocker):
    """An expired scope-update flow must not report a provisioned user as unprovisioned."""
    check_status = _capture_registered_tools()["nc_auth_check_status"]

    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.extract_user_id_from_token",
        AsyncMock(return_value="alice"),
    )
    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(
        return_value={
            "scopes": ["files.read"],
            "app_password": "old",
            "username": "alice",
        }
    )
    storage.get_login_flow_session = AsyncMock(
        return_value={
            "poll_endpoint": "https://nc/login/v2/poll",
            "poll_token": "tok",
            "requested_scopes": ["files.read", "files.write"],
        }
    )
    storage.delete_login_flow_session = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_shared_storage",
        AsyncMock(return_value=storage),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_settings",
        return_value=MagicMock(
            nextcloud_host="https://nc", nextcloud_public_issuer_url=None
        ),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.get_nextcloud_ssl_verify",
        return_value=False,
    )

    flow_client = AsyncMock()
    flow_client.poll = AsyncMock(return_value=LoginFlowPollResult(status="expired"))
    mocker.patch(
        "nextcloud_mcp_server.server.auth_tools.LoginFlowV2Client",
        return_value=flow_client,
    )

    response = await check_status(MagicMock())

    assert response.status == "provisioned"
    assert response.scopes == ["files.read"]
    storage.delete_login_flow_session.assert_awaited_once()
