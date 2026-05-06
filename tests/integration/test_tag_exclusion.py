"""End-to-end integration tests for tag-based file exclusion (issue #710).

These exercise the full resolution pipeline against a real Nextcloud
instance:

1. Create a system tag via the WebDAV ``/systemtags`` API.
2. Create real files / a real directory and tag them.
3. Resolve the configured ``EXCLUDED_TAGS`` to paths via
   ``get_excluded_file_paths`` — this issues a real PROPFIND against
   ``/systemtags/`` and a real REPORT against the user's WebDAV root.
4. Verify ``is_path_excluded`` correctly classifies tagged files,
   descendants of tagged directories, and unrelated paths.

Unlike the unit tests in ``tests/unit/test_tag_exclusion.py``, which
mock the WebDAV layer, this test catches integration-level issues:
malformed XML responses, namespace mismatches, missing fields, and
PROPFIND/REPORT semantics that diverge from what the unit-test mocks
assume.

The MCP server layer is exercised by the unit tests (where
``EXCLUDED_TAGS`` is patched at the config layer); spinning up a fresh
MCP container with a custom env var per integration test would not
add coverage proportional to the cost.
"""

import logging
import uuid

import pytest

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.server.tag_exclusion import (
    get_excluded_file_paths,
    is_path_excluded,
)

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@pytest.fixture
async def excluded_tag_environment(nc_client: NextcloudClient):
    """Provision a tag, a tagged file, a tagged directory, and an
    untagged sibling — all in a unique per-run namespace.

    Yields a dict with the layout. Cleanup runs in reverse order:
    untag, delete files, leave the tag (no public delete API on the
    client today; tags are cheap and unique-per-run).
    """
    suffix = uuid.uuid4().hex[:8]
    tag_name = f"mcp-no-ai-{suffix}"
    test_dir = f"mcp_tag_excl_{suffix}"
    tagged_file = f"{test_dir}/SECRET.txt"
    tagged_dir = f"{test_dir}/private"
    tagged_dir_child = f"{tagged_dir}/inside.txt"
    untagged_file = f"{test_dir}/visible.txt"

    # Layout
    await nc_client.webdav.create_directory(test_dir)
    await nc_client.webdav.create_directory(tagged_dir)
    await nc_client.webdav.write_file(tagged_file, b"top secret", "text/plain")
    await nc_client.webdav.write_file(tagged_dir_child, b"inside private", "text/plain")
    await nc_client.webdav.write_file(untagged_file, b"public", "text/plain")

    # Tag definition
    tag = await nc_client.webdav.get_or_create_tag(
        name=tag_name,
        user_visible=True,
        # In production we recommend user_assignable=False; for tests we
        # keep it True so cleanup via remove_tag_from_file works under
        # the same credentials.
        user_assignable=True,
    )
    assert tag["id"] is not None, "tag creation did not return an id"

    # Tag assignments — needs file IDs
    secret_info = await nc_client.webdav.get_file_info(tagged_file)
    assert secret_info is not None
    private_info = await nc_client.webdav.get_file_info(tagged_dir)
    assert private_info is not None

    await nc_client.webdav.assign_tag_to_file(secret_info["id"], tag["id"])
    await nc_client.webdav.assign_tag_to_file(private_info["id"], tag["id"])

    yield {
        "tag_name": tag_name,
        "tag_id": tag["id"],
        "test_dir": test_dir,
        "tagged_file": tagged_file,
        "tagged_dir": tagged_dir,
        "tagged_dir_child": tagged_dir_child,
        "untagged_file": untagged_file,
        "tagged_file_id": secret_info["id"],
        "tagged_dir_id": private_info["id"],
    }

    # Cleanup
    for file_id, tag_id in (
        (secret_info["id"], tag["id"]),
        (private_info["id"], tag["id"]),
    ):
        try:
            await nc_client.webdav.remove_tag_from_file(file_id, tag_id)
        except Exception as e:
            logger.warning("failed to untag file %s: %s", file_id, e)
    try:
        await nc_client.webdav.delete_resource(test_dir)
    except Exception as e:
        logger.warning("failed to delete %s: %s", test_dir, e)


async def test_get_excluded_file_paths_resolves_real_systemtags(
    excluded_tag_environment, nc_client: NextcloudClient, mocker
):
    """``get_excluded_file_paths`` resolves a real Nextcloud system tag
    to real WebDAV paths via PROPFIND + REPORT.

    Patches ``get_excluded_tag_names`` at the module level so we can
    target our per-run tag without restarting the MCP server with a
    custom ``EXCLUDED_TAGS`` env var.
    """
    env = excluded_tag_environment
    mocker.patch(
        "nextcloud_mcp_server.server.tag_exclusion.get_excluded_tag_names",
        return_value=[env["tag_name"]],
    )

    excluded = await get_excluded_file_paths(nc_client.webdav)

    # Both directly-tagged entries appear (paths are normalised — no
    # leading slashes).
    assert env["tagged_file"].lstrip("/") in excluded
    assert env["tagged_dir"].lstrip("/") in excluded

    # Descendants of the tagged directory are NOT in the resolved set
    # by themselves — they are blocked at check time via prefix match.
    assert env["tagged_dir_child"].lstrip("/") not in excluded

    # Untagged sibling is not in the set.
    assert env["untagged_file"].lstrip("/") not in excluded


async def test_is_path_excluded_against_real_resolved_set(
    excluded_tag_environment, nc_client: NextcloudClient, mocker
):
    """End-to-end: real tag → real PROPFIND/REPORT → ``is_path_excluded``
    classifies real paths correctly. Covers exact match, descendant of
    tagged directory, and unrelated path against an untagged sibling.
    """
    env = excluded_tag_environment
    mocker.patch(
        "nextcloud_mcp_server.server.tag_exclusion.get_excluded_tag_names",
        return_value=[env["tag_name"]],
    )

    excluded = await get_excluded_file_paths(nc_client.webdav)

    # Exact match on the tagged file.
    assert is_path_excluded(env["tagged_file"], excluded) is True

    # Exact match on the tagged directory.
    assert is_path_excluded(env["tagged_dir"], excluded) is True

    # A child of the tagged directory is excluded by prefix match (this
    # is the descendant-of-tagged-dir case that ``get_excluded_file_paths``
    # alone would NOT cover; the prefix matching in ``is_path_excluded``
    # is what makes recursive exclusion work).
    assert is_path_excluded(env["tagged_dir_child"], excluded) is True

    # The untagged sibling file is NOT excluded.
    assert is_path_excluded(env["untagged_file"], excluded) is False

    # A path outside the test directory is NOT excluded.
    assert is_path_excluded("/this-path-was-never-created", excluded) is False


async def test_feature_disabled_returns_empty_set_against_real_server(
    excluded_tag_environment, nc_client: NextcloudClient, mocker
):
    """With ``EXCLUDED_TAGS`` empty, ``get_excluded_file_paths`` is a
    no-op even when tags exist on the server. Verifies the early-exit
    short-circuit still holds against a real instance.
    """
    mocker.patch(
        "nextcloud_mcp_server.server.tag_exclusion.get_excluded_tag_names",
        return_value=[],
    )

    excluded = await get_excluded_file_paths(nc_client.webdav)

    assert excluded == set()
