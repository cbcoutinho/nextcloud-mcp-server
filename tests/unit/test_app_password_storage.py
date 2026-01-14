"""
Unit tests for App Password Storage functionality.

Tests the app password methods in RefreshTokenStorage for multi-user
BasicAuth mode background sync.
"""

import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit


@pytest.fixture
def encryption_key():
    """Generate a test encryption key."""
    return Fernet.generate_key().decode()


@pytest.fixture
async def temp_storage(encryption_key):
    """Create temporary storage instance with encryption for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_app_passwords.db"
        storage = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=encryption_key
        )
        await storage.initialize()
        yield storage


async def test_store_app_password(temp_storage):
    """Test storing an app password."""
    await temp_storage.store_app_password(
        user_id="testuser",
        app_password="JHWzB-ZYgLZ-3qBDj-ZQe5o-LdKpB",
    )

    # Verify it can be retrieved
    retrieved = await temp_storage.get_app_password("testuser")
    assert retrieved == "JHWzB-ZYgLZ-3qBDj-ZQe5o-LdKpB"


async def test_store_app_password_replaces_existing(temp_storage):
    """Test that storing a new app password replaces the existing one."""
    await temp_storage.store_app_password(
        user_id="testuser",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
    )
    await temp_storage.store_app_password(
        user_id="testuser",
        app_password="fffff-ggggg-hhhhh-iiiii-jjjjj",
    )

    retrieved = await temp_storage.get_app_password("testuser")
    assert retrieved == "fffff-ggggg-hhhhh-iiiii-jjjjj"


async def test_get_app_password_nonexistent(temp_storage):
    """Test retrieving app password for non-existent user."""
    retrieved = await temp_storage.get_app_password("nonexistent")
    assert retrieved is None


async def test_delete_app_password(temp_storage):
    """Test deleting an app password."""
    await temp_storage.store_app_password(
        user_id="testuser",
        app_password="JHWzB-ZYgLZ-3qBDj-ZQe5o-LdKpB",
    )

    deleted = await temp_storage.delete_app_password("testuser")
    assert deleted is True

    # Verify it's gone
    retrieved = await temp_storage.get_app_password("testuser")
    assert retrieved is None


async def test_delete_app_password_nonexistent(temp_storage):
    """Test deleting non-existent app password."""
    deleted = await temp_storage.delete_app_password("nonexistent")
    assert deleted is False


async def test_get_all_app_password_user_ids(temp_storage):
    """Test listing all users with app passwords."""
    await temp_storage.store_app_password("alice", "aaaaa-aaaaa-aaaaa-aaaaa-aaaaa")
    await temp_storage.store_app_password("bob", "bbbbb-bbbbb-bbbbb-bbbbb-bbbbb")
    await temp_storage.store_app_password("charlie", "ccccc-ccccc-ccccc-ccccc-ccccc")

    user_ids = await temp_storage.get_all_app_password_user_ids()
    assert len(user_ids) == 3
    assert "alice" in user_ids
    assert "bob" in user_ids
    assert "charlie" in user_ids


async def test_get_all_app_password_user_ids_empty(temp_storage):
    """Test listing users when none have app passwords."""
    user_ids = await temp_storage.get_all_app_password_user_ids()
    assert len(user_ids) == 0


async def test_app_password_encryption(encryption_key):
    """Test that app passwords are encrypted at rest."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_encryption.db"
        storage = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=encryption_key
        )
        await storage.initialize()

        # Store a password
        test_password = "JHWzB-ZYgLZ-3qBDj-ZQe5o-LdKpB"
        await storage.store_app_password("testuser", test_password)

        # Read directly from database to verify encryption
        import aiosqlite

        async with aiosqlite.connect(str(db_path)) as db:
            async with db.execute(
                "SELECT encrypted_password FROM app_passwords WHERE user_id = ?",
                ("testuser",),
            ) as cursor:
                row = await cursor.fetchone()

        # The stored value should be encrypted (not plain text)
        encrypted_bytes = row[0]
        assert encrypted_bytes != test_password.encode()
        # Encrypted data should be longer due to Fernet overhead
        assert len(encrypted_bytes) > len(test_password)


async def test_app_password_requires_encryption_key():
    """Test that app password operations require encryption key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_no_key.db"
        storage = RefreshTokenStorage(db_path=str(db_path), encryption_key=None)
        await storage.initialize()

        # Storing should fail without encryption key
        with pytest.raises(RuntimeError, match="Encryption key not configured"):
            await storage.store_app_password(
                "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )

        # Getting should also fail without encryption key
        with pytest.raises(RuntimeError, match="Encryption key not configured"):
            await storage.get_app_password("testuser")


async def test_multiple_users_independence(temp_storage):
    """Test that different users maintain independent app passwords."""
    users = ["alice", "bob", "charlie", "diana"]

    # Store unique passwords for each user
    for i, user in enumerate(users):
        password = (
            f"{user[0]}{user[0]}{user[0]}{user[0]}{user[0]}-" * 4
            + f"{user[0]}{user[0]}{user[0]}{user[0]}{user[0]}"
        )
        await temp_storage.store_app_password(user, password)

    # Verify each user has their correct password
    for user in users:
        expected = (
            f"{user[0]}{user[0]}{user[0]}{user[0]}{user[0]}-" * 4
            + f"{user[0]}{user[0]}{user[0]}{user[0]}{user[0]}"
        )
        retrieved = await temp_storage.get_app_password(user)
        assert retrieved == expected

    # Delete one user's password
    await temp_storage.delete_app_password("bob")

    # Verify other users unchanged
    for user in ["alice", "charlie", "diana"]:
        retrieved = await temp_storage.get_app_password(user)
        assert retrieved is not None

    # Verify bob's password is gone
    assert await temp_storage.get_app_password("bob") is None


async def test_app_password_with_special_characters(temp_storage):
    """Test storing passwords with various alphanumeric patterns."""
    # Nextcloud app passwords use alphanumeric characters
    passwords = [
        "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE",  # uppercase
        "aaaaa-bbbbb-ccccc-ddddd-eeeee",  # lowercase
        "12345-67890-12345-67890-12345",  # numbers
        "aB1cD-eF2gH-iJ3kL-mN4oP-qR5sT",  # mixed
    ]

    for i, password in enumerate(passwords):
        user = f"user{i}"
        await temp_storage.store_app_password(user, password)
        retrieved = await temp_storage.get_app_password(user)
        assert retrieved == password


async def test_decryption_with_wrong_key(encryption_key):
    """Test that decryption fails with wrong key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_wrong_key.db"

        # Store with original key
        storage1 = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=encryption_key
        )
        await storage1.initialize()
        await storage1.store_app_password("testuser", "JHWzB-ZYgLZ-3qBDj-ZQe5o-LdKpB")

        # Try to read with different key
        wrong_key = Fernet.generate_key().decode()
        storage2 = RefreshTokenStorage(db_path=str(db_path), encryption_key=wrong_key)
        await storage2.initialize()

        # Decryption should fail and return None (graceful handling)
        retrieved = await storage2.get_app_password("testuser")
        assert retrieved is None
