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


async def test_mcp_whiteboard_write_round_trip(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, board_dir: str
):
    """Create a board, draw on it, change and delete, and read it back."""
    path = f"{board_dir}/drawn.whiteboard"
    call = nc_mcp_client.call_tool

    created = _payload(await call("nc_whiteboard_create", {"path": path}))
    listed = _payload(await call("nc_whiteboard_list", {"path": board_dir}))
    assert path in {board["path"] for board in listed["results"]}

    etag = _payload(await call("nc_whiteboard_get", {"path": path}))["etag"]
    assert created["etag"] in (None, etag)
    added = _payload(
        await call(
            "nc_whiteboard_add_elements",
            {
                "path": path,
                "etag": etag,
                "elements": [
                    {"type": "rectangle", "id": "plan", "x": 0, "y": 0, "text": "Plan"},
                    {"type": "ellipse", "id": "ship", "x": 400, "y": 0, "text": "Ship"},
                    {"type": "arrow", "id": "go", "start": "plan", "end": "ship"},
                ],
            },
        )
    )
    assert added["element_ids"] == ["plan", "ship", "go"]

    etag = _payload(await call("nc_whiteboard_get", {"path": path}))["etag"]
    assert added["etag"] in (None, etag)
    _payload(
        await call(
            "nc_whiteboard_update_elements",
            {
                "path": path,
                "etag": etag,
                "changes": [{"id": "ship", "text": "Release", "y": 200}],
            },
        )
    )

    board = _payload(await call("nc_whiteboard_get", {"path": path}))
    by_id = {element["id"]: element for element in board["elements"]}
    assert by_id["plan"]["text"] == "Plan"
    assert (by_id["ship"]["text"], by_id["ship"]["y"]) == ("Release", 200)
    assert (by_id["go"]["start"], by_id["go"]["end"]) == ("plan", "ship")
    assert board["total_count"] == 3

    # A stale etag is refused, and the board is left as it was.
    stale = await call(
        "nc_whiteboard_delete_elements", {"path": path, "etag": etag, "ids": ["go"]}
    )
    assert stale.is_error is True
    assert "has changed since that etag" in stale.content[0].text

    _payload(
        await call(
            "nc_whiteboard_delete_elements",
            {"path": path, "etag": board["etag"], "ids": ["plan"]},
        )
    )
    board = _payload(await call("nc_whiteboard_get", {"path": path}))
    by_id = {element["id"]: element for element in board["elements"]}
    assert "plan" not in by_id
    assert "start" not in by_id["go"] and by_id["go"]["end"] == "ship"

    # Nothing was removed from the file: the deleted elements are still there.
    content, _, _ = await nc_client.webdav.read_file(path)
    stored = {e["id"]: e for e in json.loads(content)["elements"]}
    assert stored["plan"]["isDeleted"] is True

    existing = await call("nc_whiteboard_create", {"path": path})
    assert existing.is_error is True
