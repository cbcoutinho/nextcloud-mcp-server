"""Integration tests for Nextcloud Sharing API client."""

import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_create_and_delete_share(nc_client):
    """Test creating and deleting a file share."""
    # Create a test user to share with
    test_user = "testuser3"
    try:
        await nc_client.users.create_user(
            userid=test_user, password="SecureP@ssw0rd!2024TestUser"
        )
    except Exception:
        pass  # User might already exist

    # Create a test file
    file_path = "/test_share_file.txt"
    file_content = b"Test file for sharing"

    await nc_client.webdav.write_file(file_path, file_content)

    share_id = None
    try:
        # Create a share
        share_data = await nc_client.sharing.create_share(
            path=file_path,
            share_with=test_user,  # Share with test user
            share_type=0,  # User share
            permissions=1,  # Read-only
        )

        assert share_data is not None
        assert "id" in share_data
        share_id = share_data["id"]
        logger.info(f"Created share: {share_id}")

        # Get share info
        share_info = await nc_client.sharing.get_share(share_id)
        assert share_info["id"] == share_id
        assert share_info["path"] == file_path
        assert share_info["permissions"] == 1

        # List shares
        shares = await nc_client.sharing.list_shares(path=file_path)
        assert len(shares) > 0
        assert any(s["id"] == share_id for s in shares)

    finally:
        # Cleanup
        if share_id:
            await nc_client.sharing.delete_share(share_id)
            logger.info(f"Deleted share: {share_id}")

        await nc_client.webdav.delete_resource(file_path)

        # Cleanup test user
        try:
            await nc_client.users.delete_user(test_user)
        except Exception:
            pass


@pytest.mark.anyio
async def test_update_share_permissions(nc_client):
    """Test updating share permissions."""
    # Create a test user to share with
    test_user = "testuser3"
    try:
        await nc_client.users.create_user(
            userid=test_user, password="SecureP@ssw0rd!2024TestUser"
        )
    except Exception:
        pass  # User might already exist

    # Create a test file
    file_path = "/test_share_update.txt"
    file_content = b"Test file for permission updates"

    await nc_client.webdav.write_file(file_path, file_content)

    share_id = None
    try:
        # Create a share with read-only permissions
        share_data = await nc_client.sharing.create_share(
            path=file_path,
            share_with=test_user,
            share_type=0,
            permissions=1,  # Read-only
        )
        share_id = share_data["id"]

        # Update to read+write permissions
        updated_share = await nc_client.sharing.update_share(
            share_id=share_id,
            permissions=3,  # Read + Write
        )

        assert updated_share["id"] == share_id
        assert updated_share["permissions"] == 3

    finally:
        # Cleanup
        if share_id:
            await nc_client.sharing.delete_share(share_id)

        await nc_client.webdav.delete_resource(file_path)

        # Cleanup test user
        try:
            await nc_client.users.delete_user(test_user)
        except Exception:
            pass


@pytest.mark.anyio
async def test_list_shares(nc_client):
    """Test listing all shares."""
    # Create a test user to share with
    test_user = "testuser3"
    try:
        await nc_client.users.create_user(
            userid=test_user, password="SecureP@ssw0rd!2024TestUser"
        )
    except Exception:
        pass  # User might already exist

    # Create a test file
    file_path = "/test_list_shares.txt"
    file_content = b"Test file for listing shares"

    await nc_client.webdav.write_file(file_path, file_content)

    share_id = None
    try:
        # Create a share
        share_data = await nc_client.sharing.create_share(
            path=file_path,
            share_with=test_user,
            share_type=0,
            permissions=1,
        )
        share_id = share_data["id"]

        # List all shares
        all_shares = await nc_client.sharing.list_shares()
        assert len(all_shares) > 0

        # List shares for specific file
        file_shares = await nc_client.sharing.list_shares(path=file_path)
        assert len(file_shares) > 0
        assert any(s["id"] == share_id for s in file_shares)

    finally:
        # Cleanup
        if share_id:
            await nc_client.sharing.delete_share(share_id)

        await nc_client.webdav.delete_resource(file_path)

        # Cleanup test user
        try:
            await nc_client.users.delete_user(test_user)
        except Exception:
            pass
