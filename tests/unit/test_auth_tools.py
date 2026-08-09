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
from mcp.server.fastmcp import FastMCP

from nextcloud_mcp_server.auth.login_flow import LoginFlowPollResult
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES
from nextcloud_mcp_server.server.auth_tools import register_auth_tools

pytestmark = pytest.mark.unit


def _capture_registered_tools() -> dict:
    """Register the auth tools against a stub MCP and return them by name.

    ``register_auth_tools`` only uses ``@mcp.tool(...)`` decorators, so a stub
    whose ``tool()`` returns an identity decorator captures the closures without
    a real FastMCP instance.
    """
    captured: dict = {}

    class _StubMCP:
        def tool(self, *args, **kwargs):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    register_auth_tools(cast(FastMCP, _StubMCP()))
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
