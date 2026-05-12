"""Integration tests for app password provisioning via management API.

Tests the complete flow for multi-user BasicAuth and Login Flow v2 modes:
1. User stores app password via management API endpoint (or Login Flow v2 browser flow)
2. MCP server stores it locally (encrypted)
3. Background sync uses locally stored password to access Nextcloud

The earlier OAuth refresh-token background-sync path was removed in the
ADR-022 cleanup — these tests now cover the only supported path.
"""

import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.vector.oauth_sync import (
    NotProvisionedError,
    get_user_client_basic_auth,
)


@pytest.fixture
def encryption_key():
    """Generate a test encryption key."""
    return Fernet.generate_key().decode()


@pytest.fixture
async def temp_storage(encryption_key):
    """Create temporary storage instance with encryption for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_provisioning.db"
        storage = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=encryption_key
        )
        await storage.initialize()
        yield storage


@pytest.mark.integration
async def test_basic_auth_mode_uses_local_storage(temp_storage, mocker):
    """Test that BasicAuth mode uses locally stored app passwords.

    In multi-user BasicAuth mode, app passwords are stored locally
    in the MCP server's database after being provisioned via the API.
    """
    # Store an app password in local storage
    await temp_storage.store_app_password("test_user", "JHWzB-ZYgLZ-3qBDj-ZQe5o-LdKpB")

    # Call get_user_client_basic_auth with local storage
    client = await get_user_client_basic_auth(
        user_id="test_user",
        nextcloud_host="http://localhost:8080",
        storage=temp_storage,
    )

    # Verify client was created with correct credentials
    assert client is not None
    assert client.username == "test_user"


@pytest.mark.integration
async def test_basic_auth_mode_raises_error_without_app_password(temp_storage):
    """Test that BasicAuth mode raises NotProvisionedError if no app password.

    There is NO fallback to OAuth - if no app password, user must provision one.
    """
    # Don't store any app password

    # Call get_user_client_basic_auth - should raise NotProvisionedError
    with pytest.raises(NotProvisionedError) as exc_info:
        await get_user_client_basic_auth(
            user_id="test_user",
            nextcloud_host="http://localhost:8080",
            storage=temp_storage,
        )

    # Verify error message mentions app password provisioning
    assert "app password" in str(exc_info.value).lower()
    assert "test_user" in str(exc_info.value)


@pytest.mark.integration
async def test_multiple_users_basic_auth_mode(temp_storage, mocker):
    """Test that multiple users can be provisioned independently."""
    # Store app passwords for multiple users
    users = {
        "alice": "aaaaa-aaaaa-aaaaa-aaaaa-aaaaa",
        "bob": "bbbbb-bbbbb-bbbbb-bbbbb-bbbbb",
        "charlie": "ccccc-ccccc-ccccc-ccccc-ccccc",
    }

    for user_id, password in users.items():
        await temp_storage.store_app_password(user_id, password)

    # Verify each user can get a client
    for user_id in users.keys():
        client = await get_user_client_basic_auth(
            user_id=user_id,
            nextcloud_host="http://localhost:8080",
            storage=temp_storage,
        )
        assert client is not None
        assert client.username == user_id


@pytest.mark.integration
async def test_get_all_provisioned_users(temp_storage):
    """Test that we can list all provisioned users for BasicAuth mode."""
    # Store app passwords for multiple users
    await temp_storage.store_app_password("alice", "aaaaa-aaaaa-aaaaa-aaaaa-aaaaa")
    await temp_storage.store_app_password("bob", "bbbbb-bbbbb-bbbbb-bbbbb-bbbbb")

    # Get all provisioned users
    user_ids = await temp_storage.get_all_app_password_user_ids()

    assert len(user_ids) == 2
    assert "alice" in user_ids
    assert "bob" in user_ids


@pytest.mark.integration
async def test_revoke_app_password(temp_storage):
    """Test that deleting app password revokes background access."""
    # Provision user
    await temp_storage.store_app_password("alice", "aaaaa-aaaaa-aaaaa-aaaaa-aaaaa")

    # Verify user is provisioned
    user_ids = await temp_storage.get_all_app_password_user_ids()
    assert "alice" in user_ids

    # Revoke access
    deleted = await temp_storage.delete_app_password("alice")
    assert deleted is True

    # Verify user is no longer provisioned
    user_ids = await temp_storage.get_all_app_password_user_ids()
    assert "alice" not in user_ids

    # Verify get_user_client now raises NotProvisionedError
    with pytest.raises(NotProvisionedError):
        await get_user_client_basic_auth(
            user_id="alice",
            nextcloud_host="http://localhost:8080",
            storage=temp_storage,
        )
