"""End-to-end coverage of the Activity tools against a real stack (NC33+).

Exercises the incremental-discovery flow of GH #1932: bootstrap a cursor, make a
change, find it reading oldest-first from the cursor, then confirm the server's
answer to an unknown cursor is reported as a reset.
"""

import json
import uuid

import pytest
from mcp import ClientSession
from nextcloud_client import NextcloudClient

pytestmark = pytest.mark.integration


def _payload(result):
    assert result.is_error is False, f"MCP tool call failed: {result.content}"
    return json.loads(result.content[0].text)


async def _list(session: ClientSession, **args) -> dict:
    return _payload(await session.call_tool("nc_activity_list", args))


@pytest.fixture
async def folder(nc_client: NextcloudClient):
    name = f"mcp_activity_{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(name)
    try:
        yield name
    finally:
        await nc_client.webdav.delete_resource(name)


async def test_mcp_activity_list_filters(nc_mcp_client: ClientSession):
    data = _payload(await nc_mcp_client.call_tool("nc_activity_list_filters", {}))
    assert {"all", "files"} <= {f["id"] for f in data["filters"]}


async def test_mcp_activity_incremental_discovery(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, folder: str
):
    newest = await _list(nc_mcp_client, sort="desc", limit=1)
    cursor = newest["activities"][0]["activity_id"]

    path = f"{folder}/note.md"
    await nc_client.webdav.write_file(path, b"hello", "text/markdown")

    seen: list[str] = []
    since = cursor
    while True:
        page = await _list(nc_mcp_client, sort="asc", since=since, filter="files")
        assert page["cursor_reset"] is False
        for activity in page["activities"]:
            seen += activity["objects"].values()
        since = page["next_since"]
        if not page["has_more"]:
            break
    assert any(p.endswith(path) for p in seen), seen
    assert since > cursor

    # Reading on from the kept cursor never moves it backwards (other tests may
    # add activity meanwhile, so the page need not be empty).
    again = await _list(nc_mcp_client, sort="asc", since=since, filter="files")
    assert again["cursor_reset"] is False
    assert again["next_since"] >= since


async def test_mcp_activity_unknown_cursor_is_a_reset(nc_mcp_client: ClientSession):
    """NC33-35 restart at the first activity with a 200 for a cursor they don't know."""
    page = await _list(nc_mcp_client, sort="asc", since=2_000_000_000, limit=1)
    assert page["cursor_reset"] is True


async def test_mcp_activity_for_one_file(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, folder: str
):
    path = f"{folder}/single.md"
    await nc_client.webdav.write_file(path, b"one", "text/markdown")
    info = await nc_client.webdav.get_file_info(path)
    page = await _list(nc_mcp_client, object_id=int(info["id"]))
    assert page["activities"]
    assert all(a["object_type"] == "files" for a in page["activities"])
