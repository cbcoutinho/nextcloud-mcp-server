"""Unit tests for the content ETag on ``collectives_get_page``.

The tool reads a page's markdown through ``WebDAVClient.read_file``, which has
returned the file's ETag alongside the content since #1157 -- but the tool
discarded it, so a caller wanting to edit a page had no way to make the write
conditional on the version it read.

Pinned here: the ETag reaches the response exactly as ``read_file`` normalized
it (so it can go straight back into ``nc_webdav_write_file``'s ``if_match``),
and an ETag never appears without the content it identifies.
"""

from __future__ import annotations

import pytest
from httpx import HTTPStatusError, Request, Response
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.server.collectives import configure_collectives_tools

pytestmark = pytest.mark.unit


def _page(**overrides) -> dict:
    page = {
        "id": 42,
        "title": "Page",
        "emoji": None,
        "fileName": "Page.md",
        "filePath": "Sub",
        "collectivePath": ".Collectives/Wiki",
        "parentId": 7,
        "timestamp": 1_760_000_000,
        "size": 6,
        "lastUserId": "alice",
        "lastUserDisplayName": "Alice",
        "subpageOrder": [],
    }
    page.update(overrides)
    return page


@pytest.fixture
def collectives_tools():
    mcp = MCPServer("test")
    configure_collectives_tools(mcp)
    return mcp._tool_manager


@pytest.fixture
def stub_client(mocker):
    """A client whose page metadata and WebDAV read are both stubbed."""
    client = mocker.MagicMock()
    client.collectives.get_page = mocker.AsyncMock(return_value=_page())
    client.webdav.read_file = mocker.AsyncMock(
        return_value=(b"# Page\n", "text/markdown", "abc123")
    )
    mocker.patch(
        "nextcloud_mcp_server.server.collectives.get_client",
        mocker.AsyncMock(return_value=client),
    )
    # Pin the deployment mode: @require_scopes denies a context without a
    # verified token only under login-flow, which would otherwise make this
    # pass locally and fail in CI.
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


async def _get_page(tools, mocker):
    return await tools.get_tool("collectives_get_page").fn(
        ctx=mocker.MagicMock(), collective_id=1, page_id=42
    )


async def test_returns_the_etag_read_file_returned(
    collectives_tools, stub_client, mocker
):
    """The ETag is passed through untouched: ``read_file`` already normalized
    it to the form ``write_file``'s ``if_match`` expects, and a second
    normalization here could only make the two drift apart."""
    result = await _get_page(collectives_tools, mocker)

    assert result.content == "# Page\n"
    assert result.etag == "abc123"


async def test_no_etag_without_the_content_it_identifies(
    collectives_tools, stub_client, mocker
):
    """If the content cannot be read, no ETag may be returned either.

    An ETag on its own would let a caller make a "conditional" write based on
    content it never saw. (Whether a failed content read should fail the whole
    tool is a separate question; this pins only that the two travel together.)
    """
    request = Request("GET", "https://nc.example.com/remote.php/dav/files/x")
    stub_client.webdav.read_file.side_effect = HTTPStatusError(
        "404 Not Found", request=request, response=Response(404, request=request)
    )

    result = await _get_page(collectives_tools, mocker)

    assert result.content is None
    assert result.etag is None


@pytest.mark.parametrize(
    ("page_overrides", "expected_path"),
    [
        ({}, ".Collectives/Wiki/Sub/Page.md"),
        ({"filePath": "", "fileName": "Readme.md"}, ".Collectives/Wiki/Readme.md"),
    ],
    ids=["nested", "root"],
)
async def test_etag_belongs_to_the_derived_page_path(
    collectives_tools, stub_client, mocker, page_overrides, expected_path
):
    """The ETag is only usable against the path it was read from, so pin that
    path: ``collectivePath/filePath/fileName``, with an empty ``filePath``
    (root-level pages) omitted."""
    stub_client.collectives.get_page.return_value = _page(**page_overrides)

    await _get_page(collectives_tools, mocker)

    stub_client.webdav.read_file.assert_awaited_once_with(expected_path)
