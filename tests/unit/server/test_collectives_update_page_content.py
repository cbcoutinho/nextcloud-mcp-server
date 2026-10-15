"""Unit tests for ``collectives_update_page_content``.

The tool is a thin semantic wrapper: it locates the page's markdown file from
collective_id + page_id at write time and hands the write to the existing
``WebDAVClient.write_file`` with ``if_match``. What it adds is a content check
before the write, because Nextcloud keeps a file's etag when the file is
written twice within the same second (nextcloud/server#63994) -- an
``If-Match`` alone would then accept a write based on content the caller never
saw.

Pinned here: the path comes from fresh metadata, the caller's etag reaches
``write_file`` unchanged, every conflict (content changed, 412, 423) is refused
without writing or with a readable error, and there is no force path.
"""

from __future__ import annotations

import hashlib

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.server.collectives import configure_collectives_tools

pytestmark = pytest.mark.unit

CURRENT = b"# Page\n\nCurrent text.\n"
CURRENT_HASH = hashlib.sha256(CURRENT).hexdigest()


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
        "size": len(CURRENT),
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
    """A client with stubbed page metadata, current content and write."""
    client = mocker.MagicMock()
    client.collectives.get_page = mocker.AsyncMock(return_value=_page())
    client.webdav.read_file = mocker.AsyncMock(
        return_value=(CURRENT, "text/markdown", "etag-a")
    )
    client.webdav.write_file = mocker.AsyncMock(
        return_value={"status_code": 204, "etag": "etag-b"}
    )
    mocker.patch(
        "nextcloud_mcp_server.server.collectives.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.server.collectives.get_excluded_file_paths",
        mocker.AsyncMock(return_value=set()),
    )
    # Pin the deployment mode: @require_scopes denies a context without a
    # verified token only under login-flow, which would otherwise make this
    # pass locally and fail in CI.
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


async def _update(tools, mocker, **overrides):
    kwargs = {
        "collective_id": 1,
        "page_id": 42,
        "content": "# Page\n\nNew text.\n",
        "etag": "etag-a",
        "content_hash": CURRENT_HASH,
    }
    kwargs.update(overrides)
    return await tools.get_tool("collectives_update_page_content").fn(
        ctx=mocker.MagicMock(), **kwargs
    )


async def test_writes_conditionally_and_returns_the_new_revision(
    collectives_tools, stub_client, mocker
):
    """The caller's etag goes to write_file as if_match unchanged, and the
    response carries what the next update needs: the new etag and the hash of
    the content just written."""
    result = await _update(collectives_tools, mocker)

    stub_client.webdav.write_file.assert_awaited_once_with(
        ".Collectives/Wiki/Sub/Page.md",
        b"# Page\n\nNew text.\n",
        "text/markdown",
        if_match="etag-a",
    )
    assert result.etag == "etag-b"
    assert result.content_hash == hashlib.sha256(b"# Page\n\nNew text.\n").hexdigest()
    assert result.size == len(b"# Page\n\nNew text.\n")


async def test_path_is_resolved_at_write_time(collectives_tools, stub_client, mocker):
    """A page renamed or turned into a folder page since it was read is
    written at its current location, never at a path from an earlier read."""
    stub_client.collectives.get_page.return_value = _page(
        filePath="Sub/Renamed", fileName="Readme.md"
    )

    await _update(collectives_tools, mocker)

    path = stub_client.webdav.write_file.await_args.args[0]
    assert path == ".Collectives/Wiki/Sub/Renamed/Readme.md"
    stub_client.webdav.read_file.assert_awaited_once_with(path)


async def test_changed_content_is_refused_without_writing(
    collectives_tools, stub_client, mocker
):
    """The case an etag alone misses: the content differs from what the caller
    read although the etag may still match. Nothing may be written."""
    stub_client.webdav.read_file.return_value = (
        b"# Page\n\nSomeone else's edit.\n",
        "text/markdown",
        "etag-a",
    )

    with pytest.raises(ToolError) as exc_info:
        await _update(collectives_tools, mocker)

    assert "changed since it was read" in str(exc_info.value)
    assert "collectives_get_page" in str(exc_info.value)
    stub_client.webdav.write_file.assert_not_awaited()


async def test_stale_etag_is_reported_as_a_conflict(
    collectives_tools, stub_client, mocker
):
    """A 412 from the conditional write gets the same recovery instructions
    as a content mismatch."""
    stub_client.webdav.write_file.return_value = {
        "status_code": 412,
        "message": "File was modified since the given etag was read",
    }

    with pytest.raises(ToolError) as exc_info:
        await _update(collectives_tools, mocker)

    assert "changed since it was read" in str(exc_info.value)
    assert "Re-read" in str(exc_info.value)


async def test_locked_page_is_reported(collectives_tools, stub_client, mocker):
    stub_client.webdav.write_file.return_value = {"status_code": 423}

    with pytest.raises(ToolError) as exc_info:
        await _update(collectives_tools, mocker)

    assert "locked" in str(exc_info.value)


@pytest.mark.parametrize("etag", ["", "  ", "*"], ids=["empty", "blank", "star"])
async def test_there_is_no_force_path(collectives_tools, stub_client, mocker, etag):
    """An empty etag or ``*`` would turn the write unconditional: refuse it
    before any request is made."""
    with pytest.raises(ToolError):
        await _update(collectives_tools, mocker, etag=etag)

    stub_client.collectives.get_page.assert_not_awaited()
    stub_client.webdav.write_file.assert_not_awaited()


@pytest.mark.parametrize("content_hash", ["", "abc", "z" * 64])
async def test_malformed_content_hash_is_refused(
    collectives_tools, stub_client, mocker, content_hash
):
    with pytest.raises(ToolError):
        await _update(collectives_tools, mocker, content_hash=content_hash)

    stub_client.webdav.write_file.assert_not_awaited()


async def test_excluded_page_is_refused(collectives_tools, stub_client, mocker):
    """EXCLUDED_TAGS applies here as it does to nc_webdav_write_file, so this
    tool is not a way around it."""
    mocker.patch(
        "nextcloud_mcp_server.server.collectives.get_excluded_file_paths",
        mocker.AsyncMock(return_value={".Collectives/Wiki/Sub"}),
    )

    with pytest.raises(ToolError) as exc_info:
        await _update(collectives_tools, mocker)

    assert "excluded tag" in str(exc_info.value)
    stub_client.webdav.write_file.assert_not_awaited()


async def test_get_page_returns_the_hash_the_update_expects(
    collectives_tools, stub_client, mocker
):
    """collectives_get_page's content_hash is exactly what the update compares
    against, so the pair round-trips without the caller computing anything."""
    page = await collectives_tools.get_tool("collectives_get_page").fn(
        ctx=mocker.MagicMock(), collective_id=1, page_id=42
    )

    await _update(
        collectives_tools, mocker, etag=page.etag, content_hash=page.content_hash
    )

    stub_client.webdav.write_file.assert_awaited_once()
