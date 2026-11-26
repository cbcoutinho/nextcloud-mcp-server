"""End-to-end tests for the trash-bin and file-version MCP tools.

Exercises the real DAV endpoints: delete a file and restore it from the
trash, and overwrite a file and roll it back to its previous version.
"""

import json
import logging
import uuid

import anyio
import pytest
from mcp import ClientSession
from nextcloud_client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


def _payload(tool_result) -> dict:
    """Return the JSON-decoded text content of an MCP tool result."""
    return json.loads(tool_result.content[0].text)


async def _overwrite(nc_client: NextcloudClient, path: str, content: bytes) -> None:
    """Overwrite ``path`` so the previous content becomes a stored version.

    Nextcloud keys a version by the file's mtime in whole seconds. Two writes
    within the same second would give the old version and the new current
    revision the same key, so wait out the second first.
    """
    await anyio.sleep(1.1)
    _, _, etag = await nc_client.webdav.read_file(path)
    await nc_client.webdav.write_file(path, content, "text/plain", if_match=etag)


@pytest.fixture
async def throwaway_file(nc_client: NextcloudClient):
    """A file the tests may delete or overwrite, cleaned up afterwards."""
    path = f"mcp_trash_{uuid.uuid4().hex[:8]}.txt"
    await nc_client.webdav.write_file(path, b"first", "text/plain")
    yield path
    try:
        await nc_client.webdav.delete_resource(path)
    except Exception as e:
        logger.debug("Cleanup of %s: %s", path, e)


async def test_delete_then_restore_from_trash(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    throwaway_file: str,
):
    """A deleted file shows up in the trash and comes back intact."""
    await nc_client.webdav.delete_resource(throwaway_file)

    listing = await nc_mcp_client.call_tool("nc_webdav_list_trash", {})
    assert listing.is_error is False
    trash = _payload(listing)
    assert trash["success"] is True
    assert trash["total_count"] == len(trash["items"])

    entry = next((i for i in trash["items"] if i["name"] == throwaway_file), None)
    assert entry is not None, f"{throwaway_file} not found in trash"
    assert entry["original_location"] == throwaway_file

    restored = await nc_mcp_client.call_tool(
        "nc_webdav_restore_from_trash", {"entry_id": entry["id"]}
    )
    assert restored.is_error is False
    assert _payload(restored)["entry_id"] == entry["id"]

    content, _, _ = await nc_client.webdav.read_file(throwaway_file)
    assert content == b"first"


async def test_restore_from_trash_rejects_unknown_id(nc_mcp_client: ClientSession):
    """An id that is not in the trash is refused, not passed to DAV."""
    result = await nc_mcp_client.call_tool(
        "nc_webdav_restore_from_trash", {"entry_id": "does-not-exist"}
    )
    assert result.is_error is True


async def test_list_and_restore_version(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    throwaway_file: str,
):
    """Overwriting a file leaves a version that can be rolled back to."""
    await _overwrite(nc_client, throwaway_file, b"second")

    listing = await nc_mcp_client.call_tool(
        "nc_webdav_list_versions", {"path": throwaway_file}
    )
    assert listing.is_error is False
    versions = _payload(listing)
    assert versions["path"] == throwaway_file
    assert versions["total_count"] == len(versions["versions"])
    historical = [v for v in versions["versions"] if not v["is_current"]]
    if not historical:
        pytest.skip("Instance keeps no versions for this file")

    target = historical[0]["version_id"]
    restored = await nc_mcp_client.call_tool(
        "nc_webdav_restore_version",
        {"path": throwaway_file, "version_id": target},
    )
    assert restored.is_error is False
    assert _payload(restored)["restored_version"] == target

    content, _, _ = await nc_client.webdav.read_file(throwaway_file)
    assert content == b"first"


async def test_list_versions_refuses_missing_file(nc_mcp_client: ClientSession):
    """A path with no file id is refused rather than reported as empty."""
    result = await nc_mcp_client.call_tool(
        "nc_webdav_list_versions", {"path": f"nope_{uuid.uuid4().hex}.txt"}
    )
    assert result.is_error is True


async def test_list_versions_metadata_and_order(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    throwaway_file: str,
):
    """The listing is newest first, flags exactly one current revision, and
    tells the live file's ETag apart from each revision's."""
    await _overwrite(nc_client, throwaway_file, b"second")

    listing = _payload(
        await nc_mcp_client.call_tool(
            "nc_webdav_list_versions", {"path": throwaway_file}
        )
    )
    versions = listing["versions"]
    if len(versions) < 2:
        pytest.skip("Instance keeps no versions for this file")

    assert [v["is_current"] for v in versions].count(True) == 1
    assert versions[0]["is_current"] is True  # newest first
    assert versions[0]["size"] == len(b"second")
    assert versions[1]["size"] == len(b"first")
    assert versions[1]["content_type"] == "text/plain"
    assert versions[1]["revision_etag"]
    _, _, live_etag = await nc_client.webdav.read_file(throwaway_file)
    assert listing["current_etag"] == live_etag


async def test_read_historical_and_current_version(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    throwaway_file: str,
):
    """An old revision is readable without restoring it, and the current one
    is served from the file itself."""
    await _overwrite(nc_client, throwaway_file, b"second")

    versions = _payload(
        await nc_mcp_client.call_tool(
            "nc_webdav_list_versions", {"path": throwaway_file}
        )
    )["versions"]
    historical = next((v for v in versions if not v["is_current"]), None)
    current = next((v for v in versions if v["is_current"]), None)
    if historical is None or current is None:
        pytest.skip("Instance keeps no versions for this file")

    old = await nc_mcp_client.call_tool(
        "nc_webdav_read_version",
        {"path": throwaway_file, "version_id": historical["version_id"]},
    )
    assert old.is_error is False, old.content
    old_payload = _payload(old)
    assert old_payload["content"] == "first"
    assert old_payload["is_current"] is False
    assert old_payload["etag"] is None

    now = await nc_mcp_client.call_tool(
        "nc_webdav_read_version",
        {"path": throwaway_file, "version_id": current["version_id"]},
    )
    assert now.is_error is False, now.content
    now_payload = _payload(now)
    assert now_payload["content"] == "second"
    assert now_payload["is_current"] is True
    assert now_payload["etag"]

    # Reading did not touch the file.
    content, _, _ = await nc_client.webdav.read_file(throwaway_file)
    assert content == b"second"


async def test_read_version_refuses_unknown_version(
    nc_mcp_client: ClientSession, throwaway_file: str
):
    result = await nc_mcp_client.call_tool(
        "nc_webdav_read_version", {"path": throwaway_file, "version_id": "1"}
    )
    assert result.is_error is True


async def test_set_rename_and_clear_version_label(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    throwaway_file: str,
):
    """A version can be named, renamed and un-named, and the listing shows
    each state. The file itself is not touched."""
    await _overwrite(nc_client, throwaway_file, b"second")
    _, _, etag_before = await nc_client.webdav.read_file(throwaway_file)

    async def listed_label(version_id: str):
        versions = _payload(
            await nc_mcp_client.call_tool(
                "nc_webdav_list_versions", {"path": throwaway_file}
            )
        )["versions"]
        return next(v for v in versions if v["version_id"] == version_id)["label"]

    versions = _payload(
        await nc_mcp_client.call_tool(
            "nc_webdav_list_versions", {"path": throwaway_file}
        )
    )["versions"]
    historical = next((v for v in versions if not v["is_current"]), None)
    if historical is None:
        pytest.skip("Instance keeps no versions for this file")
    version_id = historical["version_id"]

    for label, expected, previous in (
        ("checkpoint <1> & co", "checkpoint <1> & co", None),
        ("renamed", "renamed", "checkpoint <1> & co"),
        ("", None, "renamed"),
    ):
        result = await nc_mcp_client.call_tool(
            "nc_webdav_set_version_label",
            {"path": throwaway_file, "version_id": version_id, "label": label},
        )
        assert result.is_error is False, result.content
        payload = _payload(result)
        assert payload["label"] == expected
        assert payload["previous_label"] == previous
        assert await listed_label(version_id) == expected

    _, _, etag_after = await nc_client.webdav.read_file(throwaway_file)
    assert etag_after == etag_before


async def test_set_label_refuses_unknown_version(
    nc_mcp_client: ClientSession, throwaway_file: str
):
    result = await nc_mcp_client.call_tool(
        "nc_webdav_set_version_label",
        {"path": throwaway_file, "version_id": "1", "label": "x"},
    )
    assert result.is_error is True
