"""Unit tests for the Whiteboard write tools: the etag check, what they save,
and what they refuse.

The edits themselves are covered in ``tests/unit/client/test_whiteboard_edit.py``.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from nextcloud_client.models.whiteboard import WHITEBOARD_MIME_TYPE
from nextcloud_client.models.whiteboard_edit import ElementChange, NewElement
from nextcloud_client.webdav import UnexpectedContentType

from nextcloud_mcp_server.server.whiteboard import configure_whiteboard_tools

pytestmark = pytest.mark.unit

MODULE = "nextcloud_mcp_server.server.whiteboard"
CTX = SimpleNamespace(request_context=SimpleNamespace())

BOARD = {
    "type": "excalidraw",
    "elements": [
        {
            "id": "box",
            "type": "rectangle",
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 50,
            "version": 3,
            "index": "a0",
            "customData": {"creator": {"uid": "alice"}, "creatorProof": "v1:x"},
        },
        {"id": "gone", "type": "ellipse", "isDeleted": True, "version": 9},
    ],
    "files": {"f1": {"dataURL": "data:image/png;base64,AAAA"}},
    "appState": {"viewBackgroundColor": "#ffffff"},
}


@pytest.fixture
def client(mocker):
    """A fake client behind the tools, with no excluded paths."""
    fake = SimpleNamespace(webdav=AsyncMock())
    fake.webdav.write_file.return_value = {"status_code": 204, "etag": "etag2"}
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    )
    mocker.patch(
        f"{MODULE}.get_settings",
        return_value=SimpleNamespace(document_spool_dir=None, webdav_write_max_mb=50.0),
    )
    mocker.patch(f"{MODULE}.get_client", AsyncMock(return_value=fake))
    mocker.patch(f"{MODULE}.get_excluded_file_paths", AsyncMock(return_value=set()))
    return fake


@pytest.fixture
def tools() -> SimpleNamespace:
    mcp = MCPServer(name="test-whiteboard-write-tools")
    configure_whiteboard_tools(mcp)
    by_name = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    return SimpleNamespace(
        create=by_name["nc_whiteboard_create"],
        add=by_name["nc_whiteboard_add_elements"],
        update=by_name["nc_whiteboard_update_elements"],
        delete=by_name["nc_whiteboard_delete_elements"],
    )


def _deliver(client, document: object, etag: str = "etag1") -> None:
    body = json.dumps(document).encode()

    async def stream_to_file(path, dest, *, max_bytes=None, expected_type=None):
        await anyio.lowlevel.checkpoint()
        assert expected_type == WHITEBOARD_MIME_TYPE
        dest.write_bytes(body)
        return len(body), WHITEBOARD_MIME_TYPE, etag

    client.webdav.stream_to_file = AsyncMock(side_effect=stream_to_file)


def _saved(client) -> dict:
    """The document the tool wrote, and check how it was written."""
    client.webdav.write_file.assert_awaited_once()
    path, content, content_type = client.webdav.write_file.await_args.args
    assert content_type == WHITEBOARD_MIME_TYPE
    assert client.webdav.write_file.await_args.kwargs == {"if_match": "etag1"}
    return json.loads(content)


def _http_error(status: int) -> httpx2.HTTPStatusError:
    request = httpx2.Request("PUT", "http://nextcloud.test/")
    return httpx2.HTTPStatusError(
        str(status), request=request, response=httpx2.Response(status, request=request)
    )


# ── Editing ─────────────────────────────────────────────────────────────


async def test_add_saves_the_board_with_everything_else_kept(client, tools):
    """Files, app state, unknown keys and deleted elements survive an edit."""
    _deliver(client, BOARD)
    result = await tools.add(
        path="b.whiteboard",
        etag="etag1",
        elements=[NewElement(type="ellipse", id="new", x=200, y=0, text="Hi")],
        ctx=CTX,
    )
    assert result.etag == "etag2"
    assert result.element_ids == ["new"]
    saved = _saved(client)
    assert {k: v for k, v in saved.items() if k != "elements"} == {
        k: v for k, v in BOARD.items() if k != "elements"
    }
    assert saved["elements"][:2] == BOARD["elements"]
    assert [e["id"] for e in saved["elements"][2:]][0] == "new"


async def test_a_wrapped_board_stays_wrapped(client, tools):
    """An older board keeps its ``data`` wrapper and the keys beside it."""
    _deliver(client, {"data": BOARD, "meta": 1})
    await tools.delete(path="b.whiteboard", etag="etag1", ids=["box"], ctx=CTX)
    saved = _saved(client)
    assert saved["meta"] == 1
    [box] = [e for e in saved["data"]["elements"] if e["id"] == "box"]
    assert box["isDeleted"] is True and box["version"] == 4
    assert box["customData"] == BOARD["elements"][0]["customData"]


async def test_update_returns_every_changed_id(client, tools):
    _deliver(client, BOARD)
    result = await tools.update(
        path="b.whiteboard",
        etag="etag1",
        changes=[ElementChange(id="box", text="Label")],
        ctx=CTX,
    )
    assert result.element_ids[0] == "box" and len(result.element_ids) == 2


async def test_a_stale_etag_is_refused_before_anything_is_written(client, tools):
    _deliver(client, BOARD, etag="newer")
    with pytest.raises(ToolError, match="has changed since that etag"):
        await tools.delete(path="b.whiteboard", etag="etag1", ids=["box"], ctx=CTX)
    client.webdav.write_file.assert_not_awaited()


async def test_a_quoted_etag_is_accepted(client, tools):
    """An etag copied with its quotes, as HTTP sends it, still matches."""
    _deliver(client, BOARD)
    await tools.delete(path="b.whiteboard", etag='"etag1"', ids=["box"], ctx=CTX)
    client.webdav.write_file.assert_awaited_once()


async def test_a_save_between_read_and_write_is_reported(client, tools):
    """The server's 412 on the conditional write means someone saved meanwhile."""
    _deliver(client, BOARD)
    client.webdav.write_file.return_value = {"status_code": 412, "message": "x"}
    with pytest.raises(ToolError, match="has changed since that etag"):
        await tools.delete(path="b.whiteboard", etag="etag1", ids=["box"], ctx=CTX)


async def test_an_impossible_edit_writes_nothing(client, tools):
    _deliver(client, BOARD)
    with pytest.raises(ToolError, match="Nothing was changed: no element 'gone'"):
        await tools.delete(path="b.whiteboard", etag="etag1", ids=["gone"], ctx=CTX)
    client.webdav.write_file.assert_not_awaited()


@pytest.mark.parametrize(
    ("max_mb", "cap", "message"),
    [(0.001, None, "WEBDAV_WRITE_MAX_MB"), (None, 1_000, "the most these tools read")],
)
async def test_a_board_too_large_to_save_is_refused(
    client, tools, mocker, max_mb, cap, message
):
    """Neither over the operator's write limit, nor too large to read back."""
    mocker.patch(
        f"{MODULE}.get_settings",
        return_value=SimpleNamespace(
            document_spool_dir=None, webdav_write_max_mb=max_mb
        ),
    )
    if cap is not None:
        mocker.patch(f"{MODULE}.MAX_BOARD_BYTES", cap)
    _deliver(client, BOARD)
    elements = [
        NewElement(type="text", x=0, y=i * 30, text="x" * 500) for i in range(5)
    ]
    with pytest.raises(ToolError, match=message):
        await tools.add(path="b.whiteboard", etag="etag1", elements=elements, ctx=CTX)
    client.webdav.write_file.assert_not_awaited()


async def test_a_board_with_nan_is_not_saved(client, tools):
    """The browser could not read it back, so the edit is refused."""
    body = b'{"elements": [{"id": "box", "type": "rectangle", "x": NaN}]}'

    async def stream_to_file(path, dest, *, max_bytes=None, expected_type=None):
        dest.write_bytes(body)
        return len(body), WHITEBOARD_MIME_TYPE, "etag1"

    client.webdav.stream_to_file = AsyncMock(side_effect=stream_to_file)
    with pytest.raises(ToolError, match="NaN or Infinity"):
        await tools.delete(path="b.whiteboard", etag="etag1", ids=["box"], ctx=CTX)
    client.webdav.write_file.assert_not_awaited()


async def test_a_file_that_is_not_a_board_is_not_edited(client, tools):
    """whiteboard.write must not change any other file."""
    client.webdav.stream_to_file.side_effect = UnexpectedContentType("x")
    with pytest.raises(ToolError, match="No whiteboard to read"):
        await tools.delete(path="notes.md", etag="etag1", ids=["box"], ctx=CTX)
    client.webdav.write_file.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "message"), [(403, "may not change"), (507, "HTTP 507")]
)
async def test_write_errors_are_reported(client, tools, status, message):
    _deliver(client, BOARD)
    client.webdav.write_file.side_effect = _http_error(status)
    with pytest.raises(ToolError, match=message):
        await tools.delete(path="b.whiteboard", etag="etag1", ids=["box"], ctx=CTX)


@pytest.mark.parametrize("path", ["../b.whiteboard", "./Private/b.whiteboard"])
async def test_edits_refuse_traversal_and_excluded_paths(client, tools, mocker, path):
    mocker.patch(
        f"{MODULE}.get_excluded_file_paths", AsyncMock(return_value={"Private"})
    )
    with pytest.raises(ToolError, match="segments|excluded tag"):
        await tools.delete(path=path, etag="etag1", ids=["box"], ctx=CTX)
    client.webdav.stream_to_file.assert_not_called()


# ── Creating ────────────────────────────────────────────────────────────


async def test_create_writes_an_empty_board_and_never_overwrites(client, tools):
    client.webdav.write_file.return_value = {"status_code": 201, "etag": "new"}
    result = await tools.create(path="Work/plan.whiteboard", ctx=CTX)
    assert result.etag == "new"
    path, content, content_type = client.webdav.write_file.await_args.args
    assert path == "Work/plan.whiteboard"
    assert json.loads(content) == {"elements": [], "scrollToContent": True}
    # No if_match: the client sends If-None-Match: *, so an existing file stays.
    assert client.webdav.write_file.await_args.kwargs == {}


@pytest.mark.parametrize("path", ["plan.json", "plan", "Work/.whiteboard", "a/..x"])
async def test_create_needs_a_whiteboard_name(client, tools, path):
    with pytest.raises(ToolError, match="must end in .whiteboard"):
        await tools.create(path=path, ctx=CTX)
    client.webdav.write_file.assert_not_awaited()


@pytest.mark.parametrize(
    ("result", "error", "message"),
    [
        ({"status_code": 412, "message": "x"}, None, "already exists"),
        ({"status_code": 423, "message": "x"}, None, "locked"),
        (None, _http_error(409), "Folder not found"),
        (None, _http_error(403), "may not change"),
    ],
)
async def test_create_reports_conflicts(client, tools, result, error, message):
    if error is not None:
        client.webdav.write_file.side_effect = error
    else:
        client.webdav.write_file.return_value = result
    with pytest.raises(ToolError, match=message):
        await tools.create(path="plan.whiteboard", ctx=CTX)
