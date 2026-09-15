"""End-to-end tests for the file-tag MCP tools.

Exercises the real systemtags collections: attach a tag to a file, read it
back, find the file through it, and detach it again.
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
async def tagged_file(nc_client: NextcloudClient):
    """A file to hang tags off, removed afterwards."""
    path = f"mcp_tags_{uuid.uuid4().hex[:8]}.txt"
    await nc_client.webdav.write_file(path, b"tag me", "text/plain")
    yield path
    try:
        await nc_client.webdav.delete_resource(path)
    except Exception as e:
        logger.warning("Failed to cleanup %s: %s", path, e)


async def test_tag_lifecycle(nc_mcp_client: ClientSession, tagged_file: str):
    """Tag a file, read it back, find it by tag, then untag it."""
    tag = f"mcp-test-{uuid.uuid4().hex[:8]}"

    tagged = await nc_mcp_client.call_tool(
        "nc_webdav_tag_file", {"path": tagged_file, "tag": tag}
    )
    assert tagged.isError is False
    assert _payload(tagged)["assigned"] is True

    read_back = await nc_mcp_client.call_tool(
        "nc_webdav_get_file_tags", {"path": tagged_file}
    )
    assert read_back.isError is False
    assert tag in [t["name"] for t in _payload(read_back)["tags"]]

    listed = await nc_mcp_client.call_tool("nc_webdav_list_tags", {})
    assert listed.isError is False
    assert tag in [t["name"] for t in _payload(listed)["tags"]]

    found = await nc_mcp_client.call_tool("nc_webdav_find_by_tag_name", {"tag": tag})
    assert found.isError is False
    by_tag = _payload(found)
    assert by_tag["total_count"] >= 1
    assert by_tag["tag_id"] is not None

    untagged = await nc_mcp_client.call_tool(
        "nc_webdav_untag_file", {"path": tagged_file, "tag": tag}
    )
    assert untagged.isError is False
    assert _payload(untagged)["assigned"] is False

    after = await nc_mcp_client.call_tool(
        "nc_webdav_get_file_tags", {"path": tagged_file}
    )
    assert tag not in [t["name"] for t in _payload(after)["tags"]]


async def test_tagging_is_idempotent(nc_mcp_client: ClientSession, tagged_file: str):
    """Applying the same tag twice leaves the same end state."""
    tag = f"mcp-test-{uuid.uuid4().hex[:8]}"
    for _ in range(2):
        result = await nc_mcp_client.call_tool(
            "nc_webdav_tag_file", {"path": tagged_file, "tag": tag}
        )
        assert result.isError is False

    read_back = await nc_mcp_client.call_tool(
        "nc_webdav_get_file_tags", {"path": tagged_file}
    )
    names = [t["name"] for t in _payload(read_back)["tags"]]
    assert names.count(tag) == 1


async def test_unknown_tag_yields_empty_result(nc_mcp_client: ClientSession):
    """Searching for a tag that does not exist is not an error."""
    result = await nc_mcp_client.call_tool(
        "nc_webdav_find_by_tag_name", {"tag": f"nope-{uuid.uuid4().hex}"}
    )
    assert result.isError is False
    payload = _payload(result)
    assert payload["total_count"] == 0
    assert payload["tag_id"] is None


async def test_untagging_an_unknown_tag_is_refused(
    nc_mcp_client: ClientSession, tagged_file: str
):
    result = await nc_mcp_client.call_tool(
        "nc_webdav_untag_file",
        {"path": tagged_file, "tag": f"nope-{uuid.uuid4().hex}"},
    )
    assert result.isError is True


async def test_tagging_a_missing_file_is_refused(nc_mcp_client: ClientSession):
    result = await nc_mcp_client.call_tool(
        "nc_webdav_tag_file",
        {"path": f"nope_{uuid.uuid4().hex}.txt", "tag": "whatever"},
    )
    assert result.isError is True
