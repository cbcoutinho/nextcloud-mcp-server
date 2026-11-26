"""Tool-layer tests for the file-version tools (GH #1931).

Registers the WebDAV tools on a fresh ``MCPServer`` and calls each tool's
function with a mocked client. Covers what the client tests cannot: which
endpoint serves the current revision vs a historical one, that a missing
historical revision is refused instead of being answered with the current
content, and that a historical read never hands back a write precondition.

Fixture shape mirrors ``tests/unit/test_webdav_comment_tools.py``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.server.webdav import configure_webdav_tools

pytestmark = pytest.mark.unit

_LIVE_MTIME = "Tue, 14 Nov 2023 22:15:00 GMT"

_VERSIONS = [
    {
        "version_id": "1700000100",
        "modified": _LIVE_MTIME,
        "size": "6",
        "content_type": "text/markdown",
        "revision_etag": "1700000100",
        "label": "",
        "author": "alice",
        "is_current": True,
    },
    {
        "version_id": "1700000000",
        "modified": "Tue, 14 Nov 2023 22:13:20 GMT",
        "size": "5",
        "content_type": "text/markdown",
        "revision_etag": "1700000000",
        "label": "checkpoint",
        "author": None,
        "is_current": False,
    },
]


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture(autouse=True)
def no_excluded_tags(mocker):
    async def fake(*_, **__):
        return set()

    return mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_excluded_file_paths", side_effect=fake
    )


def _writer(content: bytes, content_type: str, etag: str | None):
    """A stream_* stand-in that writes ``content`` to the spool target."""

    async def fake(*args, **_kwargs):
        dest = next(a for a in args if hasattr(a, "write_bytes"))
        dest.write_bytes(content)
        return len(content), content_type, etag

    return fake


@pytest.fixture
def fake_client(mocker):
    client = SimpleNamespace(webdav=AsyncMock())
    client.webdav.get_file_info.return_value = {
        "id": 42,
        "last_modified": _LIVE_MTIME,
        "etag": "live-etag",
        "is_directory": False,
    }
    client.webdav.list_versions.return_value = {
        "path": "doc.md",
        "file_id": 42,
        "versions": [dict(v) for v in _VERSIONS],
    }
    client.webdav.stream_to_file.side_effect = _writer(
        b"second", "text/markdown", "live-etag"
    )
    client.webdav.stream_version_to_file.side_effect = _writer(
        b"first", "text/markdown", "1700000000"
    )

    async def fake_get_client(_ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_client", side_effect=fake_get_client
    )
    return client


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer(name="test-webdav-version-tools")
    configure_webdav_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


@pytest.fixture
def read_version(tools):
    return tools["nc_webdav_read_version"].fn


@pytest.fixture
def list_versions(tools):
    return tools["nc_webdav_list_versions"].fn


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace())


async def test_list_versions_flags_current_and_reports_live_etag(
    list_versions, fake_client
):
    result = await list_versions(path="doc.md", ctx=_ctx())

    fake_client.webdav.list_versions.assert_awaited_once_with(
        "doc.md", file_id=42, current_modified=_LIVE_MTIME
    )
    assert result.current_etag == "live-etag"
    assert [v.is_current for v in result.versions] == [True, False]
    assert result.versions[0].author == "alice"
    # An empty label element means "no label", not a label named "".
    assert result.versions[0].label is None
    assert result.versions[1].label == "checkpoint"
    assert result.versions[1].revision_etag == "1700000000"


async def test_reads_a_historical_version_from_the_versions_endpoint(
    read_version, fake_client
):
    result = await read_version(path="doc.md", version_id="1700000000", ctx=_ctx())

    fake_client.webdav.stream_version_to_file.assert_awaited_once()
    assert fake_client.webdav.stream_version_to_file.await_args.args[:2] == (
        42,
        "1700000000",
    )
    fake_client.webdav.stream_to_file.assert_not_called()
    assert result.content == "first"
    assert result.is_current is False
    assert result.label == "checkpoint"
    # A historical revision must never look like a write precondition.
    assert result.etag is None
    assert result.revision_etag == "1700000000"


async def test_reads_the_current_revision_from_the_file(read_version, fake_client):
    result = await read_version(path="doc.md", version_id="1700000100", ctx=_ctx())

    fake_client.webdav.stream_to_file.assert_awaited_once()
    fake_client.webdav.stream_version_to_file.assert_not_called()
    assert result.content == "second"
    assert result.is_current is True
    assert result.etag == "live-etag"


async def test_current_revision_that_changed_mid_read_is_refused(
    read_version, fake_client
):
    fake_client.webdav.stream_to_file.side_effect = _writer(
        b"third", "text/markdown", "newer-etag"
    )
    with pytest.raises(ToolError, match="changed while"):
        await read_version(path="doc.md", version_id="1700000100", ctx=_ctx())


async def test_unknown_version_is_refused_before_any_download(
    read_version, fake_client
):
    with pytest.raises(ToolError, match="no version"):
        await read_version(path="doc.md", version_id="999", ctx=_ctx())
    fake_client.webdav.stream_version_to_file.assert_not_called()
    fake_client.webdav.stream_to_file.assert_not_called()


async def test_missing_historical_content_never_falls_back_to_current(
    read_version, fake_client
):
    request = httpx2.Request("GET", "https://nc/x")
    fake_client.webdav.stream_version_to_file.side_effect = httpx2.HTTPStatusError(
        "404", request=request, response=httpx2.Response(404, request=request)
    )
    with pytest.raises(ToolError, match="no stored content"):
        await read_version(path="doc.md", version_id="1700000000", ctx=_ctx())
    fake_client.webdav.stream_to_file.assert_not_called()


async def test_line_range_of_a_version(read_version, fake_client):
    fake_client.webdav.stream_version_to_file.side_effect = _writer(
        b"a\nb\nc\n", "text/markdown", "1700000000"
    )
    result = await read_version(
        path="doc.md", version_id="1700000000", ctx=_ctx(), line_start=2, line_end=2
    )
    assert result.content == "b\n"
    assert result.total_lines == 3


async def test_excluded_path_is_refused(read_version, fake_client, no_excluded_tags):
    async def excluded(*_, **__):
        return {"doc.md"}

    no_excluded_tags.side_effect = excluded
    with pytest.raises(ToolError, match="excluded tag"):
        await read_version(path="doc.md", version_id="1700000000", ctx=_ctx())
    fake_client.webdav.get_file_info.assert_not_called()


async def test_missing_file_is_refused(read_version, fake_client):
    request = httpx2.Request("PROPFIND", "https://nc/x")
    fake_client.webdav.get_file_info.side_effect = httpx2.HTTPStatusError(
        "404", request=request, response=httpx2.Response(404, request=request)
    )
    with pytest.raises(ToolError, match="File not found"):
        await read_version(path="doc.md", version_id="1700000000", ctx=_ctx())


async def test_bad_line_range_is_refused_before_any_request(read_version, fake_client):
    with pytest.raises(ToolError, match="Invalid line range"):
        await read_version(
            path="doc.md", version_id="1700000000", ctx=_ctx(), line_start=0
        )
    fake_client.webdav.get_file_info.assert_not_called()
