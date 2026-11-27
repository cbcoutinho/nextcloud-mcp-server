"""Unit tests for the Whiteboard tools: what they refuse, and paging.

Reading the board format is covered in ``tests/unit/client/test_whiteboard.py``.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx2
import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from nextcloud_client.models.whiteboard import WHITEBOARD_MIME_TYPE
from nextcloud_client.webdav import OversizeDownload, UnexpectedContentType

from nextcloud_mcp_server.server.whiteboard import (
    MAX_BOARD_BYTES,
    MAX_PAGE_CHARS,
    _refuse_traversal,
    configure_whiteboard_tools,
)

pytestmark = pytest.mark.unit

MODULE = "nextcloud_mcp_server.server.whiteboard"


@pytest.fixture
def client(mocker):
    """A fake client behind the tools, with no excluded paths."""
    fake = SimpleNamespace(webdav=AsyncMock())
    # Pin require_scopes to the BasicAuth pass-through: these tests call the
    # tool functions directly, with no transport and so no verified token.
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    )
    mocker.patch(
        f"{MODULE}.get_settings",
        return_value=SimpleNamespace(document_spool_dir=None),
    )
    mocker.patch(f"{MODULE}.get_client", AsyncMock(return_value=fake))
    mocker.patch(f"{MODULE}.get_excluded_file_paths", AsyncMock(return_value=set()))
    return fake


@pytest.fixture
def excluded(client, mocker):
    """Hide ``Private`` and everything below it."""
    mocker.patch(
        f"{MODULE}.get_excluded_file_paths", AsyncMock(return_value={"Private"})
    )


@pytest.fixture
def tools() -> SimpleNamespace:
    mcp = MCPServer(name="test-whiteboard-tools")
    configure_whiteboard_tools(mcp)
    by_name = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    return SimpleNamespace(
        list=by_name["nc_whiteboard_list"], get=by_name["nc_whiteboard_get"]
    )


CTX = SimpleNamespace(request_context=SimpleNamespace())


def _deliver(client, body: bytes, content_type: str = WHITEBOARD_MIME_TYPE) -> None:
    async def stream_to_file(path, dest, *, max_bytes=None, expected_type=None):
        await anyio.lowlevel.checkpoint()
        assert max_bytes == MAX_BOARD_BYTES
        assert expected_type == WHITEBOARD_MIME_TYPE
        if content_type.split(";")[0] != expected_type:
            raise UnexpectedContentType(path)
        dest.write_bytes(body)
        return len(body), content_type, "etag1"

    client.webdav.stream_to_file = AsyncMock(side_effect=stream_to_file)


def _board(count: int) -> bytes:
    return json.dumps(
        {"elements": [{"id": f"e{i}", "type": "rectangle"} for i in range(count)]}
    ).encode()


def _http_error(status: int) -> httpx2.HTTPStatusError:
    request = httpx2.Request("GET", "http://nextcloud.test/")
    return httpx2.HTTPStatusError(
        str(status), request=request, response=httpx2.Response(status, request=request)
    )


# ── nc_whiteboard_get ───────────────────────────────────────────────────


async def test_get_returns_elements_and_etag(client, tools):
    """A content type with parameters is still a whiteboard."""
    _deliver(client, _board(2), f"{WHITEBOARD_MIME_TYPE}; charset=utf-8")
    result = await tools.get(path="a.whiteboard", ctx=CTX)
    assert [e.id for e in result.elements] == ["e0", "e1"]
    assert result.etag == "etag1"
    assert result.total_count == 2
    assert result.next_offset is None


async def test_get_pages_cover_the_board_once(client, tools):
    """Following next_offset yields every element exactly once."""
    _deliver(client, _board(7))
    seen, offset = [], 0
    while offset is not None:
        page = await tools.get(path="a.whiteboard", ctx=CTX, offset=offset, limit=3)
        assert page.total_count == 7
        seen += [e.id for e in page.elements]
        offset = page.next_offset
    assert seen == [f"e{i}" for i in range(7)]


async def test_get_past_the_end_is_empty(client, tools):
    """An offset past the end is an empty last page, not an error."""
    _deliver(client, _board(1))
    result = await tools.get(path="a.whiteboard", ctx=CTX, offset=5)
    assert result.elements == []
    assert result.next_offset is None


async def test_get_page_is_bounded_in_characters(client, tools):
    """A page with a lot of text ends early, and no element is lost."""
    texts = [{"id": f"e{i}", "type": "text", "text": "x" * 9_000} for i in range(40)]
    _deliver(client, json.dumps({"elements": texts}).encode())
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = await tools.get(path="a.whiteboard", ctx=CTX, offset=offset)
        assert page.elements
        assert sum(len(e.model_dump_json()) for e in page.elements) <= MAX_PAGE_CHARS
        seen += [e.id for e in page.elements]
        offset = page.next_offset
        pages += 1
    assert seen == [f"e{i}" for i in range(40)]
    assert pages > 1


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", ""])
async def test_get_refuses_a_file_that_is_not_a_whiteboard(client, tools, content_type):
    """whiteboard.read must not read other files, even ones shaped like a board."""
    _deliver(client, _board(1), content_type)
    with pytest.raises(ToolError, match="No whiteboard to read"):
        await tools.get(path="a.whiteboard", ctx=CTX)


async def test_get_reports_a_damaged_board(client, tools):
    """The reason the board cannot be read reaches the caller."""
    _deliver(client, b'{"elements": [')
    with pytest.raises(ToolError, match="not a readable whiteboard: not valid JSON"):
        await tools.get(path="a.whiteboard", ctx=CTX)


async def test_get_refuses_an_excluded_path_before_reading(client, excluded, tools):
    """Nothing is downloaded for a path under EXCLUDED_TAGS."""
    with pytest.raises(ToolError, match="excluded tag"):
        await tools.get(path="/Private/a.whiteboard", ctx=CTX)
    client.webdav.stream_to_file.assert_not_called()


async def test_get_says_the_same_for_every_file_it_will_not_read(client, tools):
    """whiteboard.read must not tell which files exist, or how big they are."""
    messages = set()
    for error in (
        _http_error(404),
        _http_error(403),
        OversizeDownload("over 40 MB"),
        UnexpectedContentType("application/pdf"),
    ):
        client.webdav.stream_to_file.side_effect = error
        with pytest.raises(ToolError) as raised:
            await tools.get(path="a.whiteboard", ctx=CTX)
        messages.add(str(raised.value))
    assert len(messages) == 1
    assert "No whiteboard to read at 'a.whiteboard'" in messages.pop()


async def test_get_reports_a_server_error(client, tools):
    """A failure that says nothing about the file keeps its status."""
    client.webdav.stream_to_file.side_effect = _http_error(500)
    with pytest.raises(ToolError, match="HTTP 500"):
        await tools.get(path="a.whiteboard", ctx=CTX)


async def test_get_refuses_traversal(client, tools):
    """Refused with a clear message, before any request. Which paths are
    traversal is ``test_only_a_whole_dotdot_segment_is_traversal``."""
    with pytest.raises(ToolError, match="segments"):
        await tools.get(path="Work\\..\\a", ctx=CTX)
    client.webdav.stream_to_file.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/Private//a.whiteboard",
        "Private/./a.whiteboard",
        "./Private/a.whiteboard",
        ".\\Private\\a.whiteboard",
    ],
)
async def test_get_refuses_an_excluded_path_however_it_is_spelled(
    client, excluded, tools, path
):
    """A non-canonical spelling does not get past the exclusion."""
    with pytest.raises(ToolError, match="excluded tag"):
        await tools.get(path=path, ctx=CTX)
    client.webdav.stream_to_file.assert_not_called()


# ── nc_whiteboard_list ──────────────────────────────────────────────────


def _found(path: str) -> dict:
    return {"name": path.rsplit("/", 1)[-1], "path": path, "is_directory": False}


async def test_list_searches_by_type_in_the_folder(client, tools):
    """The search is by MIME type and scoped to the folder."""
    client.webdav.find_all_by_type.return_value = [_found("Work/a.whiteboard")]
    result = await tools.list(ctx=CTX, path="Work")
    client.webdav.find_all_by_type.assert_awaited_once_with(
        WHITEBOARD_MIME_TYPE, scope="Work"
    )
    assert [board.path for board in result.results] == ["Work/a.whiteboard"]
    assert result.total_found == 1


async def test_list_hides_excluded_boards(client, excluded, tools):
    """Boards under EXCLUDED_TAGS are not listed."""
    client.webdav.find_all_by_type.return_value = [
        _found("Private/a.whiteboard"),
        _found("b.whiteboard"),
    ]
    result = await tools.list(ctx=CTX)
    assert [board.path for board in result.results] == ["b.whiteboard"]
    assert result.total_found == 1


@pytest.mark.parametrize(
    "path", ["/Private//", "./Private", "Private/./sub", ".\\Private\\sub"]
)
async def test_list_refuses_an_excluded_folder_however_it_is_spelled(
    client, excluded, tools, path
):
    """A non-canonical spelling does not get past the exclusion."""
    with pytest.raises(ToolError, match="excluded tag"):
        await tools.list(ctx=CTX, path=path)
    client.webdav.find_all_by_type.assert_not_called()


async def test_list_refuses_an_excluded_folder(client, excluded, tools):
    """Listing inside an excluded folder is refused, unsearched."""
    with pytest.raises(ToolError, match="excluded tag"):
        await tools.list(ctx=CTX, path="Private")
    client.webdav.find_all_by_type.assert_not_called()


async def test_list_refuses_traversal(client, tools):
    """A folder path stays within the user's files, like a file path."""
    with pytest.raises(ToolError, match="segments"):
        await tools.list(ctx=CTX, path="/../other")
    client.webdav.find_all_by_type.assert_not_called()


async def test_list_allows_dots_in_names(client, tools):
    client.webdav.find_all_by_type.return_value = []
    result = await tools.list(ctx=CTX, path="a..b")
    assert result.total_found == 0


#: Dot-heavy segments, so "..", "...", "." and "a..b" all turn up.
_segment = st.text(alphabet="a.", min_size=1, max_size=4)


@st.composite
def _paths(draw) -> tuple[str, bool]:
    """A path in ``/`` or ``\\`` spelling, and whether it has a ``..`` segment."""
    traverses = draw(st.booleans())
    segments = draw(st.lists(_segment.filter(lambda s: s != ".."), max_size=4))
    if traverses:
        segments.insert(draw(st.integers(0, len(segments))), "..")
    path = "".join(s + draw(st.sampled_from("/\\")) for s in segments)
    return draw(st.sampled_from(["", "/"])) + path[:-1], traverses


@given(_paths())
@example(("Work/../../a", True))
@example(("Work\\..\\..", True))
@example(("..hidden", False))
@example(("Work...", False))
def test_only_a_whole_dotdot_segment_is_traversal(case):
    path, traverses = case
    if traverses:
        with pytest.raises(ToolError, match="segments"):
            _refuse_traversal(path)
    else:
        _refuse_traversal(path)


@pytest.mark.parametrize(
    ("status", "message"), [(404, "Folder not found"), (500, "HTTP 500")]
)
async def test_list_translates_errors(client, tools, status, message):
    """Search failures become messages the model can act on."""
    client.webdav.find_all_by_type.side_effect = _http_error(status)
    with pytest.raises(ToolError, match=message):
        await tools.list(ctx=CTX, path="Work")
