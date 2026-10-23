"""Server-layer tests for ``nc_webdav_edit_file`` and line-range reads.

The edit logic itself is covered in ``test_text_edit.py``. These pin the
wiring: what is downloaded, what is written back and under which etag, and
that every refusal leaves the file untouched (no PUT at all).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.client.webdav import OversizeDownload
from nextcloud_mcp_server.models.webdav import EditFileResponse, FileEdit
from nextcloud_mcp_server.server.webdav import configure_webdav_tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path.

    Same reason as in ``test_webdav_tools_exclusion.py``: the tools are called
    directly, without a transport or verified token.
    """
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer(name="test-webdav-edit")
    configure_webdav_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "document_read_timeout_seconds": None,
        "document_spool_dir": None,
        "document_max_pdf_size_mb": 50.0,
        "webdav_write_max_mb": 50.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def webdav(mocker):
    """A fake WebDAV client: downloads ``state["body"]``, records writes."""
    state = {"body": b"", "content_type": "text/markdown", "etag": "e1"}
    client = SimpleNamespace(webdav=AsyncMock())

    async def stream_to_file(path, dest, *, max_bytes=None):
        await anyio.lowlevel.checkpoint()
        body = state["body"]
        if max_bytes is not None and len(body) > max_bytes:
            raise OversizeDownload(f"{path} exceeded {max_bytes} bytes")
        dest.write_bytes(body)
        return len(body), state["content_type"], state["etag"]

    client.webdav.stream_to_file = AsyncMock(side_effect=stream_to_file)
    client.webdav.write_file = AsyncMock(
        return_value={"status_code": 204, "etag": "e2"}
    )

    async def fake_get_client(ctx):
        return client

    async def no_exclusions(*_, **__):
        return set()

    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", side_effect=fake_get_client
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_excluded_file_paths",
        side_effect=no_exclusions,
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings", return_value=_settings()
    )
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.astrolabe_browser_base", return_value=None
    )
    state["client"] = client
    return state


def _ctx() -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.request_context = SimpleNamespace()
    return ctx


def _edit(old: str, new: str, replace_all: bool = False) -> FileEdit:
    return FileEdit(old_string=old, new_string=new, replace_all=replace_all)


# ── nc_webdav_edit_file ─────────────────────────────────────────────────


async def test_edit_writes_back_under_the_etag_it_read(tools, webdav):
    """The PUT is conditional on the etag of the download it is based on."""
    webdav["body"] = b"# Title\n\nold line\n"

    result = await tools["nc_webdav_edit_file"].fn(
        path="/Notes/page.md", edits=[_edit("old line", "new line")], ctx=_ctx()
    )

    assert isinstance(result, EditFileResponse)
    assert result.etag == "e2"
    assert result.edits_applied == 1
    assert result.replacements == 1
    assert result.size == len(b"# Title\n\nnew line\n")
    assert "-old line" in result.diff.splitlines()
    assert "+new line" in result.diff.splitlines()
    webdav["client"].webdav.write_file.assert_awaited_once_with(
        "/Notes/page.md", b"# Title\n\nnew line\n", "text/markdown", if_match="e1"
    )


async def test_several_edits_are_one_write(tools, webdav):
    """One PUT, so one new file version, however many edits the call carries."""
    webdav["body"] = b"a\nb\nc\n"

    result = await tools["nc_webdav_edit_file"].fn(
        path="/f.txt", edits=[_edit("a", "A"), _edit("c", "C")], ctx=_ctx()
    )

    assert result.edits_applied == 2
    webdav["client"].webdav.write_file.assert_awaited_once()
    assert webdav["client"].webdav.write_file.await_args.args[1] == b"A\nb\nC\n"


async def test_a_failing_edit_writes_nothing(tools, webdav):
    """All or nothing: the first edit would apply, the second does not."""
    webdav["body"] = b"a\nb\n"
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("a", "A"), _edit("missing", "x")]

    with pytest.raises(ToolError, match="Edit 2 of 2.*Nothing was changed"):
        await edit_file(path="/f.txt", edits=edits, ctx=ctx)

    webdav["client"].webdav.write_file.assert_not_awaited()


async def test_ambiguous_match_writes_nothing(tools, webdav):
    webdav["body"] = b"x\nx\n"
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("x", "y")]

    with pytest.raises(ToolError, match="occurs 2 times"):
        await edit_file(path="/f.txt", edits=edits, ctx=ctx)

    webdav["client"].webdav.write_file.assert_not_awaited()


async def test_excluded_path_is_neither_read_nor_written(tools, webdav, mocker):
    async def excluded(*_, **__):
        return {"Secret.txt"}

    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_excluded_file_paths",
        side_effect=excluded,
    )
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("a", "b")]

    with pytest.raises(ToolError, match="excluded tag"):
        await edit_file(path="/Secret.txt", edits=edits, ctx=ctx)

    webdav["client"].webdav.stream_to_file.assert_not_awaited()
    webdav["client"].webdav.write_file.assert_not_awaited()


async def test_non_utf8_file_is_refused(tools, webdav):
    webdav["body"] = b"\x89PNG\r\n\x1a\n\xff\xfe"
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("PNG", "JPG")]

    with pytest.raises(ToolError, match="not a UTF-8 text file"):
        await edit_file(path="/img.png", edits=edits, ctx=ctx)

    webdav["client"].webdav.write_file.assert_not_awaited()


@pytest.mark.parametrize("given", ["e1", '"e1"', 'W/"e1"'])
async def test_matching_if_match_is_accepted_in_any_quoting(tools, webdav, given):
    webdav["body"] = b"a\n"

    await tools["nc_webdav_edit_file"].fn(
        path="/f.txt", edits=[_edit("a", "b")], ctx=_ctx(), if_match=given
    )

    webdav["client"].webdav.write_file.assert_awaited_once()


async def test_stale_if_match_writes_nothing(tools, webdav):
    """The caller based the edit on a version that is no longer current."""
    webdav["body"] = b"a\n"
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("a", "b")]

    with pytest.raises(ToolError, match="changed since you read it"):
        await edit_file(path="/f.txt", edits=edits, ctx=ctx, if_match="old-etag")

    webdav["client"].webdav.write_file.assert_not_awaited()


async def test_missing_etag_is_refused(tools, webdav):
    """Without an etag the write-back could clobber a concurrent change."""
    webdav["body"] = b"a\n"
    webdav["etag"] = None
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("a", "b")]

    with pytest.raises(ToolError, match="no ETag"):
        await edit_file(path="/f.txt", edits=edits, ctx=ctx)

    webdav["client"].webdav.write_file.assert_not_awaited()


@pytest.mark.parametrize(
    ("write_result", "expected"),
    [
        (
            {"status_code": 412, "message": "File was modified"},
            "changed by someone else",
        ),
        ({"status_code": 423, "message": "File is locked"}, "File is locked"),
    ],
)
async def test_write_conflicts_surface_as_errors(tools, webdav, write_result, expected):
    webdav["body"] = b"a\n"
    webdav["client"].webdav.write_file.return_value = write_result
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("a", "b")]

    with pytest.raises(ToolError, match=expected):
        await edit_file(path="/f.txt", edits=edits, ctx=ctx)


async def test_missing_file_is_reported_as_not_found(tools, webdav):
    request = httpx.Request("GET", "http://test.local/remote.php/dav/files/u/x")
    response = httpx.Response(404, request=request)
    webdav["client"].webdav.stream_to_file.side_effect = httpx.HTTPStatusError(
        "404", request=request, response=response
    )
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("a", "b")]

    with pytest.raises(ToolError, match="File not found"):
        await edit_file(path="/x.txt", edits=edits, ctx=ctx)


async def test_file_over_the_write_limit_is_refused(tools, webdav, mocker):
    """A file that could not be written back is not downloaded in full either."""
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_settings",
        return_value=_settings(webdav_write_max_mb=0.000001),
    )
    webdav["body"] = b"a" * 100
    edit_file = tools["nc_webdav_edit_file"].fn
    ctx = _ctx()
    edits = [_edit("a", "b", replace_all=True)]

    with pytest.raises(ToolError, match="too large to edit"):
        await edit_file(path="/big.txt", edits=edits, ctx=ctx)

    webdav["client"].webdav.write_file.assert_not_awaited()


def test_edit_tool_registration(tools):
    """ADR-017 update semantics, and the write scope, like nc_webdav_write_file."""
    tool = tools["nc_webdav_edit_file"]

    assert tool.title == "Edit File"
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is True
    assert tool.annotations.read_only_hint is not True
    assert list(tool.fn._required_scopes) == ["files.write"]


def test_write_tool_points_to_the_edit_tool(tools):
    """Agents pick tools by description, so the full-rewrite tool names the
    cheaper one for partial changes."""
    assert "nc_webdav_edit_file" in tools["nc_webdav_write_file"].description


# ── nc_webdav_read_file line ranges ─────────────────────────────────────


async def test_read_line_range(tools, webdav):
    webdav["body"] = b"one\ntwo\nthree\nfour\n"
    webdav["content_type"] = "text/plain"

    result = await tools["nc_webdav_read_file"].fn(
        path="/f.txt", ctx=_ctx(), line_start=2, line_end=3
    )

    assert result.content == "two\nthree\n"
    assert (result.line_start, result.line_end, result.total_lines) == (2, 3, 4)
    assert result.etag == "e1"


async def test_read_line_range_with_numbers(tools, webdav):
    webdav["body"] = b"one\ntwo\n"
    webdav["content_type"] = "text/plain"

    result = await tools["nc_webdav_read_file"].fn(
        path="/f.txt", ctx=_ctx(), line_start=2, line_numbers=True
    )

    assert result.content == "     2\ttwo"


async def test_read_line_range_works_for_non_text_mime_types(tools, webdav):
    """JSON is UTF-8 text even though its MIME type is not text/*."""
    webdav["body"] = b'{\n  "a": 1\n}\n'
    webdav["content_type"] = "application/json"

    result = await tools["nc_webdav_read_file"].fn(
        path="/f.json", ctx=_ctx(), line_start=2, line_end=2
    )

    assert result.content == '  "a": 1\n'


async def test_read_line_range_past_the_end(tools, webdav):
    webdav["body"] = b"one\n"
    webdav["content_type"] = "text/plain"
    read_file = tools["nc_webdav_read_file"].fn
    ctx = _ctx()

    with pytest.raises(ToolError, match="past the end"):
        await read_file(path="/f.txt", ctx=ctx, line_start=5)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"line_start": 0}, "numbered from 1"),
        ({"line_start": 3, "line_end": 2}, "precedes"),
        ({"line_start": 1, "page_start": 1}, "not both"),
    ],
)
async def test_invalid_line_ranges_are_rejected_before_any_download(
    tools, webdav, kwargs, expected
):
    read_file = tools["nc_webdav_read_file"].fn
    ctx = _ctx()

    with pytest.raises(ToolError, match=expected):
        await read_file(path="/f.txt", ctx=ctx, **kwargs)

    webdav["client"].webdav.stream_to_file.assert_not_awaited()
