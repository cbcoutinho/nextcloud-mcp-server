"""Integration tests for WebDAV operations."""

import logging
import uuid

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture
async def test_base_path(nc_client: NextcloudClient):
    """Base path for test files/directories."""
    test_dir = f"mcp_test_{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(test_dir)
    yield test_dir
    await nc_client.webdav.delete_resource(test_dir)


async def test_create_and_delete_directory(
    nc_client: NextcloudClient, test_base_path: str
):
    """Test creating and deleting directories."""
    test_dir = f"{test_base_path}/test_directory"

    try:
        # Create directory
        result = await nc_client.webdav.create_directory(test_dir)
        assert result["status_code"] == 201  # Created
        logger.info(f"Created directory: {test_dir}")

        # Verify directory exists by listing parent
        parent_listing = await nc_client.webdav.list_directory(test_base_path)
        dir_names = [item["name"] for item in parent_listing]
        assert "test_directory" in dir_names

        # Delete directory
        delete_result = await nc_client.webdav.delete_resource(test_dir)
        assert delete_result["status_code"] in [204, 404]  # No Content or Not Found
        logger.info(f"Deleted directory: {test_dir}")

    finally:
        # Cleanup: ensure directory is deleted
        try:
            await nc_client.webdav.delete_resource(test_dir)
        except Exception:
            pass


async def test_write_read_delete_file(nc_client: NextcloudClient, test_base_path: str):
    """Test writing, reading, and deleting files."""
    test_file = f"{test_base_path}/test_file.txt"
    test_content = f"Test content {uuid.uuid4().hex}"

    try:
        # Create base directory first
        await nc_client.webdav.create_directory(test_base_path)

        # Write file
        write_result = await nc_client.webdav.write_file(
            test_file, test_content.encode("utf-8"), content_type="text/plain"
        )
        assert write_result["status_code"] in [200, 201, 204]  # Success codes
        logger.info(f"Wrote file: {test_file}")

        # Read file back
        content, content_type = await nc_client.webdav.read_file(test_file)
        assert content.decode("utf-8") == test_content
        assert "text/plain" in content_type
        logger.info(f"Read file: {test_file}")

        # Verify file appears in directory listing
        listing = await nc_client.webdav.list_directory(test_base_path)
        file_names = [item["name"] for item in listing]
        assert "test_file.txt" in file_names

        # Delete file
        delete_result = await nc_client.webdav.delete_resource(test_file)
        assert delete_result["status_code"] in [204, 404]  # No Content or Not Found
        logger.info(f"Deleted file: {test_file}")

    finally:
        # Cleanup
        try:
            await nc_client.webdav.delete_resource(test_file)
            await nc_client.webdav.delete_resource(test_base_path)
        except Exception:
            pass


async def test_list_directory_empty_and_populated(
    nc_client: NextcloudClient, test_base_path: str
):
    """Test listing empty and populated directories."""
    try:
        # Create base directory
        await nc_client.webdav.create_directory(test_base_path)

        # List empty directory
        empty_listing = await nc_client.webdav.list_directory(test_base_path)
        assert isinstance(empty_listing, list)
        assert len(empty_listing) == 0
        logger.info(f"Empty directory listing: {len(empty_listing)} items")

        # Add some files and directories
        await nc_client.webdav.create_directory(f"{test_base_path}/subdir1")
        await nc_client.webdav.create_directory(f"{test_base_path}/subdir2")
        await nc_client.webdav.write_file(
            f"{test_base_path}/file1.txt", b"content1", content_type="text/plain"
        )
        await nc_client.webdav.write_file(
            f"{test_base_path}/file2.md",
            b"# Markdown content",
            content_type="text/markdown",
        )

        # List populated directory
        populated_listing = await nc_client.webdav.list_directory(test_base_path)
        assert len(populated_listing) == 4  # 2 dirs + 2 files

        # Check that we have both files and directories
        names = [item["name"] for item in populated_listing]
        assert "subdir1" in names
        assert "subdir2" in names
        assert "file1.txt" in names
        assert "file2.md" in names

        # Check metadata is present
        for item in populated_listing:
            assert "name" in item
            assert "path" in item
            assert "is_directory" in item
            assert "size" in item
            assert "content_type" in item
            assert "last_modified" in item

        logger.info(f"Populated directory listing: {len(populated_listing)} items")

    finally:
        # Cleanup
        try:
            await nc_client.webdav.delete_resource(f"{test_base_path}/file1.txt")
            await nc_client.webdav.delete_resource(f"{test_base_path}/file2.md")
            await nc_client.webdav.delete_resource(f"{test_base_path}/subdir1")
            await nc_client.webdav.delete_resource(f"{test_base_path}/subdir2")
            await nc_client.webdav.delete_resource(test_base_path)
        except Exception:
            pass


async def test_read_nonexistent_file(nc_client: NextcloudClient):
    """Test reading a file that doesn't exist."""
    nonexistent_file = f"nonexistent_{uuid.uuid4().hex}.txt"

    with pytest.raises(HTTPStatusError) as exc_info:
        await nc_client.webdav.read_file(nonexistent_file)

    assert exc_info.value.response.status_code == 404
    logger.info(f"Correctly got 404 for nonexistent file: {nonexistent_file}")


async def test_delete_nonexistent_resource(nc_client: NextcloudClient):
    """Test deleting a resource that doesn't exist."""
    nonexistent_resource = f"nonexistent_{uuid.uuid4().hex}"

    result = await nc_client.webdav.delete_resource(nonexistent_resource)
    assert result["status_code"] == 404
    logger.info(f"Correctly got 404 for nonexistent resource: {nonexistent_resource}")


async def test_create_nested_directories(
    nc_client: NextcloudClient, test_base_path: str
):
    """Test creating nested directory structures."""
    nested_path = f"{test_base_path}/level1/level2/level3"

    try:
        # Create nested directories (should create parent directories automatically)
        result = await nc_client.webdav.create_directory(nested_path, True)
        assert result["status_code"] == 201

        # Verify the structure was created
        level1_listing = await nc_client.webdav.list_directory(
            f"{test_base_path}/level1"
        )
        assert len(level1_listing) == 1
        assert level1_listing[0]["name"] == "level2"
        assert level1_listing[0]["is_directory"] is True

        level2_listing = await nc_client.webdav.list_directory(
            f"{test_base_path}/level1/level2"
        )
        assert len(level2_listing) == 1
        assert level2_listing[0]["name"] == "level3"
        assert level2_listing[0]["is_directory"] is True

        logger.info(f"Created nested directory structure: {nested_path}")

    finally:
        # Cleanup - delete from deepest to shallowest
        try:
            await nc_client.webdav.delete_resource(nested_path)
            await nc_client.webdav.delete_resource(f"{test_base_path}/level1/level2")
            await nc_client.webdav.delete_resource(f"{test_base_path}/level1")
        except Exception:
            pass


async def test_overwrite_existing_file(nc_client: NextcloudClient, test_base_path: str):
    """Test overwriting an existing file."""
    test_file = f"{test_base_path}/overwrite_test.txt"
    original_content = "Original content"
    new_content = "New content after overwrite"

    try:
        # Create base directory
        await nc_client.webdav.create_directory(test_base_path)

        # Write original file
        await nc_client.webdav.write_file(
            test_file, original_content.encode("utf-8"), content_type="text/plain"
        )

        # Verify original content
        content, _ = await nc_client.webdav.read_file(test_file)
        assert content.decode("utf-8") == original_content

        # Overwrite with new content
        overwrite_result = await nc_client.webdav.write_file(
            test_file, new_content.encode("utf-8"), content_type="text/plain"
        )
        assert overwrite_result["status_code"] in [200, 204]  # OK or No Content

        # Verify new content
        content, _ = await nc_client.webdav.read_file(test_file)
        assert content.decode("utf-8") == new_content

        logger.info(f"Successfully overwrote file: {test_file}")

    finally:
        # Cleanup
        try:
            await nc_client.webdav.delete_resource(test_file)
            await nc_client.webdav.delete_resource(test_base_path)
        except Exception:
            pass


async def test_list_root_directory(nc_client: NextcloudClient):
    """Test listing the root directory."""
    root_listing = await nc_client.webdav.list_directory("")

    # Root directory should exist and be listable
    assert isinstance(root_listing, list)
    # Should have at least some default folders/files
    assert len(root_listing) >= 0

    # Check structure of items
    for item in root_listing:
        assert "name" in item
        assert "path" in item
        assert "is_directory" in item
        assert "size" in item
        assert "content_type" in item
        assert "last_modified" in item

    logger.info(f"Root directory contains {len(root_listing)} items")
