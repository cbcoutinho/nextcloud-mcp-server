"""End-to-end tests for the trash-bin and file-version MCP tools.

Exercises the real DAV endpoints: delete a file and restore it from the
trash, and overwrite a file and roll it back to its previous version.
"""

import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


def _payload(tool_result) -> dict:
    """Return the JSON-decoded text content of an MCP tool result."""
    return json.loads(tool_result.content[0].text)


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
    assert listing.isError is False
    trash = _payload(listing)
    assert trash["success"] is True
    assert trash["total_count"] == len(trash["items"])

    entry = next((i for i in trash["items"] if i["name"] == throwaway_file), None)
    assert entry is not None, f"{throwaway_file} not found in trash"
    assert entry["original_location"] == throwaway_file

    restored = await nc_mcp_client.call_tool(
        "nc_webdav_restore_from_trash", {"entry_id": entry["id"]}
    )
    assert restored.isError is False
    assert _payload(restored)["entry_id"] == entry["id"]

    content, _, _ = await nc_client.webdav.read_file(throwaway_file)
    assert content == b"first"


async def test_restore_from_trash_rejects_unknown_id(nc_mcp_client: ClientSession):
    """An id that is not in the trash is refused, not passed to DAV."""
    result = await nc_mcp_client.call_tool(
        "nc_webdav_restore_from_trash", {"entry_id": "does-not-exist"}
    )
    assert result.isError is True


async def test_list_and_restore_version(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    throwaway_file: str,
):
    """Overwriting a file leaves a version that can be rolled back to."""
    _, _, etag = await nc_client.webdav.read_file(throwaway_file)
    await nc_client.webdav.write_file(
        throwaway_file, b"second", "text/plain", if_match=etag
    )

    listing = await nc_mcp_client.call_tool(
        "nc_webdav_list_versions", {"path": throwaway_file}
    )
    assert listing.isError is False
    versions = _payload(listing)
    assert versions["path"] == throwaway_file
    assert versions["total_count"] == len(versions["versions"])
    if not versions["versions"]:
        pytest.skip("Instance keeps no versions for this file")

    target = versions["versions"][0]["version_id"]
    restored = await nc_mcp_client.call_tool(
        "nc_webdav_restore_version",
        {"path": throwaway_file, "version_id": target},
    )
    assert restored.isError is False
    assert _payload(restored)["restored_version"] == target

    content, _, _ = await nc_client.webdav.read_file(throwaway_file)
    assert content == b"first"


async def test_list_versions_refuses_missing_file(nc_mcp_client: ClientSession):
    """A path with no file id is refused rather than reported as empty."""
    result = await nc_mcp_client.call_tool(
        "nc_webdav_list_versions", {"path": f"nope_{uuid.uuid4().hex}.txt"}
    )
    assert result.isError is True
