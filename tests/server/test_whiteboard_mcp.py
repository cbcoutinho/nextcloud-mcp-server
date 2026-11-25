"""End-to-end coverage of the Whiteboard MCP tools against a real stack.

Needs the Whiteboard app: enabling it registers the MIME type that the tools
recognise a board by.
"""

import json
import uuid

import pytest
from mcp import ClientSession
from nextcloud_client import NextcloudClient

pytestmark = pytest.mark.integration

BOARD = {
    "elements": [
        {"id": "a", "type": "rectangle", "x": 0, "y": 0, "width": 99.6, "height": 50},
        {"id": "a-label", "type": "text", "text": "Start", "containerId": "a"},
        {"id": "b", "type": "ellipse", "x": 300, "y": 0, "width": 100, "height": 50},
        {
            "id": "ab",
            "type": "arrow",
            "x": 100,
            "y": 25,
            "points": [[0, 0], [200, 0]],
            "startBinding": {"elementId": "a", "focus": 0, "gap": 4},
            "endBinding": {"elementId": "b", "focus": 0, "gap": 4},
        },
        {"id": "gone", "type": "diamond", "isDeleted": True},
    ],
    "files": {},
}


def _payload(result):
    assert result.is_error is False, f"MCP tool call failed: {result.content}"
    return json.loads(result.content[0].text)


@pytest.fixture
async def board_dir(nc_client: NextcloudClient):
    """A folder with a board, an empty board and a JSON file shaped like one."""
    folder = f"mcp_whiteboard_{uuid.uuid4().hex[:8]}"
    content = json.dumps(BOARD).encode()
    await nc_client.webdav.create_directory(folder)
    try:
        # The content type of the upload is not what Nextcloud reports later:
        # it detects the type from the file name.
        for name, body in (
            ("board.whiteboard", content),
            ("empty.whiteboard", b""),
            ("not-a-board.json", content),
        ):
            await nc_client.webdav.write_file(
                f"{folder}/{name}", body, "application/octet-stream"
            )
        yield folder
    finally:
        await nc_client.webdav.delete_resource(folder)


async def test_mcp_whiteboard_list(nc_mcp_client: ClientSession, board_dir: str):
    """Boards are listed; a JSON file shaped like one is not."""
    data = _payload(
        await nc_mcp_client.call_tool("nc_whiteboard_list", {"path": board_dir})
    )
    assert {board["path"] for board in data["results"]} == {
        f"{board_dir}/board.whiteboard",
        f"{board_dir}/empty.whiteboard",
    }
    assert data["total_found"] == 2


async def test_mcp_whiteboard_get(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, board_dir: str
):
    """The compact view of a real file, with the etag of that file."""
    path = f"{board_dir}/board.whiteboard"
    result = await nc_mcp_client.call_tool("nc_whiteboard_get", {"path": path})
    data = _payload(result)

    assert data["elements"] == [
        {
            "id": "a",
            "type": "rectangle",
            "text": "Start",
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 50,
        },
        {"id": "b", "type": "ellipse", "x": 300, "y": 0, "width": 100, "height": 50},
        {"id": "ab", "type": "arrow", "x": 100, "y": 25, "start": "a", "end": "b"},
    ]
    assert result.structured_content["elements"] == data["elements"]
    assert data["total_count"] == 3
    _, _, etag = await nc_client.webdav.read_file(path)
    assert data["etag"] == etag


async def test_mcp_whiteboard_get_empty_board(
    nc_mcp_client: ClientSession, board_dir: str
):
    """A new board, which is an empty file, has no elements."""
    data = _payload(
        await nc_mcp_client.call_tool(
            "nc_whiteboard_get", {"path": f"{board_dir}/empty.whiteboard"}
        )
    )
    assert data["elements"] == []
    assert data["total_count"] == 0


async def test_mcp_whiteboard_get_refuses_anything_else_alike(
    nc_mcp_client: ClientSession, board_dir: str
):
    """A JSON file shaped like a board, a missing file, and the folder itself.

    The message must not tell them apart: whiteboard.read does not grant
    knowing which other files exist.
    """
    messages = set()
    for name in ("not-a-board.json", "missing.whiteboard", ""):
        path = f"{board_dir}/{name}"
        result = await nc_mcp_client.call_tool("nc_whiteboard_get", {"path": path})
        assert result.is_error is True
        messages.add(result.content[0].text.replace(path, "PATH"))
    assert len(messages) == 1, messages
    assert "No whiteboard to read" in messages.pop()
