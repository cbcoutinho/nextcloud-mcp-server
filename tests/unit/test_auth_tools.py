"""Unit tests for Login Flow v2 MCP auth tools.

Tests the auth tools logic with mocked storage and Login Flow client.
"""

import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES

pytestmark = pytest.mark.unit


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
        scopes=["notes:read", "notes:write"],
        username="alice_nc",
    )

    data = await temp_storage.get_app_password_with_scopes("alice")
    assert data is not None
    assert data["app_password"] == "aaaaa-bbbbb-ccccc-ddddd-eeeee"
    assert data["scopes"] == ["notes:read", "notes:write"]
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
        scopes=["notes:read"],
    )
    await temp_storage.store_app_password_with_scopes(
        user_id="alice",
        app_password="xxxxx-yyyyy-zzzzz-aaaaa-bbbbb",
        scopes=["notes:read", "calendar:read"],
        username="alice_nc",
    )

    data = await temp_storage.get_app_password_with_scopes("alice")
    assert data["app_password"] == "xxxxx-yyyyy-zzzzz-aaaaa-bbbbb"
    assert data["scopes"] == ["notes:read", "calendar:read"]


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
        requested_scopes=["notes:read", "notes:write"],
    )

    session = await temp_storage.get_login_flow_session("alice")
    assert session is not None
    assert session["poll_token"] == "secret-poll-token"
    assert session["poll_endpoint"] == "https://cloud.example.com/login/v2/poll"
    assert session["requested_scopes"] == ["notes:read", "notes:write"]
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
    """Test that ALL_SUPPORTED_SCOPES contains expected scopes."""
    assert "notes:read" in ALL_SUPPORTED_SCOPES
    assert "notes:write" in ALL_SUPPORTED_SCOPES
    assert "calendar:read" in ALL_SUPPORTED_SCOPES
    assert "files:read" in ALL_SUPPORTED_SCOPES
    assert "deck:read" in ALL_SUPPORTED_SCOPES
    # Scopes should be in pairs (read/write)
    read_scopes = [s for s in ALL_SUPPORTED_SCOPES if s.endswith(":read")]
    write_scopes = [s for s in ALL_SUPPORTED_SCOPES if s.endswith(":write")]
    assert len(read_scopes) == len(write_scopes)
