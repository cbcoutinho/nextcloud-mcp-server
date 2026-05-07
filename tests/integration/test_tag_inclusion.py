"""End-to-end integration tests for tag-based file inclusion in
``NextcloudClient.find_files_by_tag``.

The vector scanner relies on this helper to enumerate files under the
``vector-index`` system tag (env: ``VECTOR_SYNC_PDF_TAG``). A user can
tag either an individual file *or* a folder; in the folder case the
tag should propagate to every matching descendant via a
``Depth: infinity`` WebDAV SEARCH.

Mirror of ``test_tag_exclusion.py`` but for the *inclusion* path.
Catches integration-level issues that the unit tests in
``tests/unit/client/test_nextcloud_client.py`` cannot, such as
PROPFIND/REPORT/SEARCH semantics, Nextcloud's actual MIME-type
reporting for the test fixtures, and the order in which directly-tagged
files vs descendants are returned.
"""

import logging
import uuid

import pytest

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@pytest.fixture
async def included_tag_environment(nc_client: NextcloudClient):
    """Provision a tag, a directly-tagged file, a tagged directory with
    a child file, and an untagged sibling — all in a unique per-run
    namespace.

    Yields a dict with the layout. Cleanup runs in reverse order: untag,
    delete files. The tag itself is left behind (no public delete-tag
    API on the client today; tags are cheap and unique-per-run).
    """
    suffix = uuid.uuid4().hex[:8]
    tag_name = f"mcp-include-{suffix}"
    test_dir = f"mcp_tag_incl_{suffix}"
    tagged_file = f"{test_dir}/tagged.txt"
    tagged_dir = f"{test_dir}/inside_dir"
    tagged_dir_child = f"{tagged_dir}/child.txt"
    nested_dir = f"{tagged_dir}/nested"
    nested_dir_child = f"{nested_dir}/deep.txt"
    untagged_file = f"{test_dir}/untagged.txt"

    await nc_client.webdav.create_directory(test_dir)
    await nc_client.webdav.create_directory(tagged_dir)
    await nc_client.webdav.create_directory(nested_dir)
    await nc_client.webdav.write_file(tagged_file, b"tagged file", "text/plain")
    await nc_client.webdav.write_file(
        tagged_dir_child, b"child of tagged dir", "text/plain"
    )
    await nc_client.webdav.write_file(
        nested_dir_child, b"deep nested under tagged dir", "text/plain"
    )
    await nc_client.webdav.write_file(untagged_file, b"untagged sibling", "text/plain")

    tag = await nc_client.webdav.get_or_create_tag(
        name=tag_name, user_visible=True, user_assignable=True
    )
    assert tag["id"] is not None, "tag creation did not return an id"

    tagged_file_info = await nc_client.webdav.get_file_info(tagged_file)
    tagged_dir_info = await nc_client.webdav.get_file_info(tagged_dir)
    assert tagged_file_info is not None and tagged_dir_info is not None

    await nc_client.webdav.assign_tag_to_file(tagged_file_info["id"], tag["id"])
    await nc_client.webdav.assign_tag_to_file(tagged_dir_info["id"], tag["id"])

    yield {
        "tag_name": tag_name,
        "tag_id": tag["id"],
        "test_dir": test_dir,
        "tagged_file": tagged_file,
        "tagged_file_id": tagged_file_info["id"],
        "tagged_dir": tagged_dir,
        "tagged_dir_id": tagged_dir_info["id"],
        "tagged_dir_child": tagged_dir_child,
        "nested_dir_child": nested_dir_child,
        "untagged_file": untagged_file,
    }

    for file_id in (tagged_file_info["id"], tagged_dir_info["id"]):
        try:
            await nc_client.webdav.remove_tag_from_file(file_id, tag["id"])
        except Exception as e:
            logger.warning("failed to untag file %s: %s", file_id, e)
    try:
        await nc_client.webdav.delete_resource(test_dir)
    except Exception as e:
        logger.warning("failed to delete %s: %s", test_dir, e)


def _basenames(files: list[dict]) -> set[str]:
    """Return the basename of each file path for assertion convenience."""
    return {f["path"].rstrip("/").rsplit("/", 1)[-1] for f in files}


async def test_find_files_by_tag_includes_directly_tagged_file(
    included_tag_environment, nc_client: NextcloudClient
):
    """A file with the tag directly applied is returned, regardless of the
    folder-walk machinery."""
    env = included_tag_environment

    files = await nc_client.find_files_by_tag(
        env["tag_name"], mime_type_filter="text/plain"
    )

    names = _basenames(files)
    assert "tagged.txt" in names
    # untagged sibling outside any tagged directory must not appear
    assert "untagged.txt" not in names


async def test_find_files_by_tag_expands_tagged_directory(
    included_tag_environment, nc_client: NextcloudClient
):
    """A tagged folder applies its tag to every matching descendant via
    Depth: infinity SEARCH — including deeply nested files."""
    env = included_tag_environment

    files = await nc_client.find_files_by_tag(
        env["tag_name"], mime_type_filter="text/plain"
    )

    names = _basenames(files)
    # Direct child of the tagged folder
    assert "child.txt" in names
    # Grandchild — proves the walk is recursive, not single-level
    assert "deep.txt" in names


async def test_find_files_by_tag_dedupes_directly_tagged_files_under_tagged_folder(
    included_tag_environment, nc_client: NextcloudClient
):
    """When a file is *both* directly tagged and lives under a tagged
    folder, it is returned exactly once. Verifies the dedup-by-id path."""
    env = included_tag_environment

    # Tag the deep child directly so it appears via two paths.
    deep_info = await nc_client.webdav.get_file_info(env["nested_dir_child"])
    assert deep_info is not None
    await nc_client.webdav.assign_tag_to_file(deep_info["id"], env["tag_id"])

    try:
        files = await nc_client.find_files_by_tag(
            env["tag_name"], mime_type_filter="text/plain"
        )
    finally:
        await nc_client.webdav.remove_tag_from_file(deep_info["id"], env["tag_id"])

    ids = [f["id"] for f in files]
    assert ids.count(deep_info["id"]) == 1, f"deep child returned more than once: {ids}"


async def test_find_files_by_tag_excludes_unrelated_paths(
    included_tag_environment, nc_client: NextcloudClient
):
    """A file in the same parent directory as the tagged folder, but not
    under it, is not returned. Guards against an over-broad SEARCH scope."""
    env = included_tag_environment

    files = await nc_client.find_files_by_tag(
        env["tag_name"], mime_type_filter="text/plain"
    )

    paths = {f["path"].lstrip("/") for f in files}
    assert env["untagged_file"] not in paths
