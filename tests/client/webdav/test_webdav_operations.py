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
        logger.info("Created directory: %s", test_dir)

        # Verify directory exists by listing parent
        parent_listing = await nc_client.webdav.list_directory(test_base_path)
        dir_names = [item["name"] for item in parent_listing]
        assert "test_directory" in dir_names

        # Delete directory
        delete_result = await nc_client.webdav.delete_resource(test_dir)
        assert delete_result["status_code"] in [204, 404]  # No Content or Not Found
        logger.info("Deleted directory: %s", test_dir)

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
        logger.info("Wrote file: %s", test_file)

        # Read file back
        content, content_type, etag = await nc_client.webdav.read_file(test_file)
        assert content.decode("utf-8") == test_content
        assert "text/plain" in content_type
        assert etag
        logger.info("Read file: %s", test_file)

        # Verify file appears in directory listing
        listing = await nc_client.webdav.list_directory(test_base_path)
        file_names = [item["name"] for item in listing]
        assert "test_file.txt" in file_names

        # Delete file
        delete_result = await nc_client.webdav.delete_resource(test_file)
        assert delete_result["status_code"] in [204, 404]  # No Content or Not Found
        logger.info("Deleted file: %s", test_file)

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
        logger.info("Empty directory listing: %s items", len(empty_listing))

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

        logger.info("Populated directory listing: %s items", len(populated_listing))

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
    logger.info("Correctly got 404 for nonexistent file: %s", nonexistent_file)


async def test_delete_nonexistent_resource(nc_client: NextcloudClient):
    """Test deleting a resource that doesn't exist."""
    nonexistent_resource = f"nonexistent_{uuid.uuid4().hex}"

    result = await nc_client.webdav.delete_resource(nonexistent_resource)
    assert result["status_code"] == 404
    logger.info("Correctly got 404 for nonexistent resource: %s", nonexistent_resource)


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

        logger.info("Created nested directory structure: %s", nested_path)

    finally:
        # Cleanup - delete from deepest to shallowest
        try:
            await nc_client.webdav.delete_resource(nested_path)
            await nc_client.webdav.delete_resource(f"{test_base_path}/level1/level2")
            await nc_client.webdav.delete_resource(f"{test_base_path}/level1")
        except Exception:
            pass


async def test_overwrite_existing_file(nc_client: NextcloudClient, test_base_path: str):
    """Writes are fail-closed: an existing file can only be overwritten with a
    matching etag (safe) or if_match='*' (force). An etag-less write over an
    existing file fails; a stale etag fails; the fresh etag succeeds."""
    test_file = f"{test_base_path}/overwrite_test.txt"
    original_content = "Original content"
    new_content = "New content after overwrite"

    try:
        # Create base directory
        await nc_client.webdav.create_directory(test_base_path)

        # Write original file (create-only default; the path does not exist yet)
        await nc_client.webdav.write_file(
            test_file, original_content.encode("utf-8"), content_type="text/plain"
        )

        # Verify original content and capture the etag
        content, _, original_etag = await nc_client.webdav.read_file(test_file)
        assert content.decode("utf-8") == original_content
        assert original_etag

        # An etag-less write over the now-existing file is refused (fail-closed).
        create_conflict = await nc_client.webdav.write_file(
            test_file, new_content.encode("utf-8"), content_type="text/plain"
        )
        assert create_conflict["status_code"] == 412
        # Content is untouched.
        content, _, _ = await nc_client.webdav.read_file(test_file)
        assert content.decode("utf-8") == original_content

        # A stale etag is also refused.
        stale = await nc_client.webdav.write_file(
            test_file,
            new_content.encode("utf-8"),
            content_type="text/plain",
            if_match="deadbeef-not-the-real-etag",
        )
        assert stale["status_code"] == 412

        # The fresh etag succeeds.
        overwrite_result = await nc_client.webdav.write_file(
            test_file,
            new_content.encode("utf-8"),
            content_type="text/plain",
            if_match=original_etag,
        )
        assert overwrite_result["status_code"] in [200, 204]  # OK or No Content

        # Verify new content
        content, _, _ = await nc_client.webdav.read_file(test_file)
        assert content.decode("utf-8") == new_content

        # if_match='*' force-overwrites an existing file unconditionally.
        force_result = await nc_client.webdav.write_file(
            test_file, b"Forced", content_type="text/plain", if_match="*"
        )
        assert force_result["status_code"] in [200, 204]

        logger.info("Successfully overwrote file: %s", test_file)

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

    logger.info("Root directory contains %s items", len(root_listing))


async def test_write_returns_etag_usable_without_reread(
    nc_client: NextcloudClient, test_base_path: str
):
    """A write's ETag must be directly reusable as the next write's if_match.

    Chaining edits previously required a re-GET between them, which is also what
    widened the concurrent-edit window the precondition exists to narrow. The
    contract this pins is exactly that: the value comes back, it is accepted as a
    precondition with no intervening read, and read_file reports the same thing.

    Deliberately NOT asserted: that the ETag *changes* between writes. It did not
    in CI — two writes with different content and different lengths both returned
    the same value — so that is an assumption about Nextcloud's ETag derivation,
    not part of this feature's contract. The stale-etag case below therefore uses
    a fabricated value, matching how test_overwrite_existing_file already does it.
    """
    path = f"{test_base_path}/etag-chain-{uuid.uuid4().hex[:8]}.txt"

    created = await nc_client.webdav.write_file(path, b"v1", "text/plain")
    assert created["status_code"] in (201, 204)
    assert created["etag"], "server did not return an ETag on create"

    # The point of the feature: the returned etag is accepted as a precondition
    # with no read between the two writes.
    second = await nc_client.webdav.write_file(
        path, b"v2-longer-body", "text/plain", if_match=created["etag"]
    )
    assert second["status_code"] in (200, 204)
    assert second["etag"], "server did not return an ETag on overwrite"

    content, _, read_etag = await nc_client.webdav.read_file(path)
    assert content == b"v2-longer-body"
    # read_file and write_file must agree on the representation — the reason both
    # go through _normalize_etag.
    assert read_etag == second["etag"]

    # A value that was never this file's etag must be refused, and must not
    # overwrite what is there.
    stale = await nc_client.webdav.write_file(
        path, b"v3", "text/plain", if_match="deadbeef-not-the-real-etag"
    )
    assert stale["status_code"] == 412

    content_after, _, _ = await nc_client.webdav.read_file(path)
    assert content_after == b"v2-longer-body", "a rejected write must not overwrite"

    await nc_client.webdav.delete_resource(path)


async def test_move_with_destination_etag_guards_the_destination(
    nc_client: NextcloudClient, test_base_path: str
):
    """The acceptance test for the destination precondition.

    This is the check that matters: `If-Match` on a MOVE would guard the SOURCE
    and pass happily while clobbering the destination, so a unit test asserting
    header bytes proves only that we send something. Only a live server shows the
    condition is actually enforced.

    The mismatching case uses a fabricated etag rather than a superseded one.
    Nextcloud does not reliably change a file's ETag between writes (observed in
    CI: two writes with different content and length returned the same value), so
    "write again to make the etag stale" is not a dependable way to produce a
    mismatch. A value that was never the destination's etag is unambiguous.
    """
    suffix = uuid.uuid4().hex[:8]
    src = f"{test_base_path}/dest-guard-src-{suffix}.txt"
    dst = f"{test_base_path}/dest-guard-dst-{suffix}.txt"

    source_body = b"source-payload"
    dest_body = b"destination-payload"

    await nc_client.webdav.write_file(src, source_body, "text/plain")
    await nc_client.webdav.write_file(dst, dest_body, "text/plain")

    # A non-matching destination etag must stop the move.
    blocked = await nc_client.webdav.move_resource(
        src, dst, overwrite=True, if_destination_match="deadbeef-not-the-real-etag"
    )
    assert blocked["status_code"] == 412, (
        "a non-matching destination etag did not block the move — the If: header "
        "is a no-op and the precondition is not being enforced"
    )

    # Nothing moved: the destination keeps its content and the source survives.
    dst_content, _, dst_etag = await nc_client.webdav.read_file(dst)
    assert dst_content == dest_body, "destination was clobbered despite a mismatch"
    src_content, _, _ = await nc_client.webdav.read_file(src)
    assert src_content == source_body, "source was moved despite a blocked MOVE"

    # With the destination's real etag the move goes through.
    ok = await nc_client.webdav.move_resource(
        src, dst, overwrite=True, if_destination_match=dst_etag
    )
    assert ok["status_code"] in (201, 204)
    moved, _, _ = await nc_client.webdav.read_file(dst)
    assert moved == source_body

    await nc_client.webdav.delete_resource(dst)


async def test_directory_destination_always_fails_the_etag_check(
    nc_client: NextcloudClient, test_base_path: str
):
    """sabre evaluates the etag condition only for `$node instanceof IFile`, so a
    directory destination can never satisfy it.

    Pinned so the documented limitation is a known quantity — if a future sabre
    starts honouring collection etags, this test fails and tells us.
    """
    suffix = uuid.uuid4().hex[:8]
    src = f"{test_base_path}/dir-guard-src-{suffix}.txt"
    dst_dir = f"{test_base_path}/dir-guard-dst-{suffix}"

    await nc_client.webdav.write_file(src, b"payload", "text/plain")
    await nc_client.webdav.create_directory(dst_dir)

    result = await nc_client.webdav.move_resource(
        src, dst_dir, overwrite=True, if_destination_match="anything"
    )

    assert result["status_code"] == 412

    await nc_client.webdav.delete_resource(src)
    await nc_client.webdav.delete_resource(dst_dir)
