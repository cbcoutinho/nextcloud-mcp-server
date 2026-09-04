"""End-to-end tests for the WebDAV file-comment MCP tools (GH #1308).

Exercises the real DAV comments collection through the MCP server: post a
comment on a file, read it back, and page over it.
"""

import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@pytest.fixture
async def commented_file(nc_client: NextcloudClient):
    """A file to hang comments off, removed afterwards."""
    path = f"mcp_comments_{uuid.uuid4().hex[:8]}.txt"
    await nc_client.webdav.write_file(path, b"review me", "text/plain")
    yield path
    try:
        await nc_client.webdav.delete_resource(path)
    except Exception as e:
        logger.warning("Failed to cleanup %s: %s", path, e)


def _data(result):
    return json.loads(result.content[0].text)


async def test_create_and_list_comment(
    nc_mcp_client: ClientSession, commented_file: str
):
    """A posted comment comes back from the list tool, verbatim."""
    message = f"Automated check {uuid.uuid4().hex[:8]}"

    created = _data(
        await nc_mcp_client.call_tool(
            "nc_webdav_create_comment",
            arguments={"path": commented_file, "message": message},
        )
    )
    assert created["success"] is True
    assert created["file_id"] > 0
    assert created["comment_id"] is not None

    listed = _data(
        await nc_mcp_client.call_tool(
            "nc_webdav_list_comments", arguments={"path": commented_file}
        )
    )
    assert listed["count"] == 1
    comment = listed["results"][0]
    assert comment["id"] == created["comment_id"]
    assert comment["message"] == message
    assert comment["verb"] == "comment"
    assert comment["actor_type"] == "users"
    assert comment["actor_id"]


async def test_list_comments_paging(nc_mcp_client: ClientSession, commented_file: str):
    """limit/offset page over the thread, newest first."""
    for i in range(3):
        await nc_mcp_client.call_tool(
            "nc_webdav_create_comment",
            arguments={"path": commented_file, "message": f"comment {i}"},
        )

    first_page = _data(
        await nc_mcp_client.call_tool(
            "nc_webdav_list_comments", arguments={"path": commented_file, "limit": 2}
        )
    )
    assert first_page["count"] == 2
    assert first_page["results"][0]["message"] == "comment 2"

    second_page = _data(
        await nc_mcp_client.call_tool(
            "nc_webdav_list_comments",
            arguments={"path": commented_file, "limit": 2, "offset": 2},
        )
    )
    assert second_page["count"] == 1
    assert second_page["results"][0]["message"] == "comment 0"


async def test_comment_on_missing_file_is_refused(nc_mcp_client: ClientSession):
    """A path that resolves to nothing is a clear refusal, not a raw 404."""
    result = await nc_mcp_client.call_tool(
        "nc_webdav_create_comment",
        arguments={"path": f"no_such_file_{uuid.uuid4().hex}.txt", "message": "hi"},
    )

    assert result.is_error
    assert "File not found" in result.content[0].text
