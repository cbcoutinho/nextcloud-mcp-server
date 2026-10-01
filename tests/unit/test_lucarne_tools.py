"""Tool-layer tests for the Lucarne tools.

They register the tools on a fresh ``MCPServer`` and call each tool's underlying
function directly with a mocked client, mirroring
``tests/unit/test_deck_comment_overflow.py``. What they cover is what the client
tests cannot: that add/remove merge into the catalogue's current channels rather
than replacing them, and how HTTP failures read to the model.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.server.lucarne import configure_lucarne_tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer(name="test-lucarne-tools")
    configure_lucarne_tools(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


@pytest.fixture
def lucarne(mocker) -> AsyncMock:
    """The mocked ``client.lucarne`` that every tool reaches through get_client."""
    client = SimpleNamespace(lucarne=AsyncMock())

    async def fake_get_client(ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.lucarne.get_client", side_effect=fake_get_client
    )
    return client.lucarne


@pytest.fixture
def ctx() -> SimpleNamespace:
    return SimpleNamespace()


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://nc/test")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request)
    )


def test_registers_the_expected_tools(tools):
    assert set(tools) == {
        "nc_lucarne_list_channels",
        "nc_lucarne_subscribe_channel",
        "nc_lucarne_add_video_to_playlist",
        "nc_lucarne_list_catalogs",
        "nc_lucarne_get_catalog",
        "nc_lucarne_create_catalog",
        "nc_lucarne_update_catalog",
        "nc_lucarne_delete_catalog",
        "nc_lucarne_add_channels_to_catalog",
        "nc_lucarne_remove_channels_from_catalog",
        "nc_lucarne_list_playlists",
        "nc_lucarne_create_playlist",
        "nc_lucarne_update_playlist",
        "nc_lucarne_delete_playlist",
    }


async def test_add_channels_keeps_the_ones_already_filed(tools, lucarne, ctx):
    lucarne.get_catalog.return_value = {"id": 1, "name": "Tech", "channel_ids": [5, 2]}
    lucarne.replace_catalog_channels.return_value = {
        "id": 1,
        "name": "Tech",
        "channel_ids": [2, 3, 5],
    }

    result = await tools["nc_lucarne_add_channels_to_catalog"](1, [3, 2], ctx)

    lucarne.replace_catalog_channels.assert_awaited_once_with(1, [2, 3, 5])
    assert result.catalog.channel_ids == [2, 3, 5]


async def test_remove_channels_only_takes_out_the_named_ones(tools, lucarne, ctx):
    lucarne.get_catalog.return_value = {
        "id": 1,
        "name": "Tech",
        "channel_ids": [2, 3, 5],
    }
    lucarne.replace_catalog_channels.return_value = {
        "id": 1,
        "name": "Tech",
        "channel_ids": [2, 5],
    }

    await tools["nc_lucarne_remove_channels_from_catalog"](1, [3, 99], ctx)

    lucarne.replace_catalog_channels.assert_awaited_once_with(1, [2, 5])


async def test_remove_channels_from_unknown_catalog_does_not_write(tools, lucarne, ctx):
    lucarne.get_catalog.side_effect = _http_error(404)
    remove = tools["nc_lucarne_remove_channels_from_catalog"]

    with pytest.raises(MCPError, match="not found"):
        await remove(1, [3], ctx)

    lucarne.replace_catalog_channels.assert_not_awaited()


async def test_create_catalog_name_clash_reads_as_conflict(tools, lucarne, ctx):
    lucarne.create_catalog.side_effect = _http_error(409)
    create = tools["nc_lucarne_create_catalog"]

    with pytest.raises(MCPError, match="conflict"):
        await create("Tech", ctx)


async def test_network_error_is_reported(tools, lucarne, ctx):
    lucarne.get_catalogs.side_effect = httpx.ConnectError("down")
    list_catalogs = tools["nc_lucarne_list_catalogs"]

    with pytest.raises(MCPError, match="Network error"):
        await list_catalogs(ctx)


async def test_list_channels_passes_filters_through(tools, lucarne, ctx):
    lucarne.get_channels.return_value = [
        {"id": 1, "title": "A", "source_url": "https://youtube.com/@a"}
    ]

    result = await tools["nc_lucarne_list_channels"](
        ctx, catalog_id=None, uncategorized=True
    )

    lucarne.get_channels.assert_awaited_once_with(None, True)
    assert result.total_count == 1


async def test_delete_playlist_reports_it_is_queued(tools, lucarne, ctx):
    lucarne.delete_playlist.return_value = {"queued": True}

    result = await tools["nc_lucarne_delete_playlist"](7, ctx, delete_videos=True)

    lucarne.delete_playlist.assert_awaited_once_with(7, True)
    assert result.playlist_id == 7
    assert result.queued is True


async def test_subscribe_channel_returns_the_new_channel(tools, lucarne, ctx):
    lucarne.add_channel.return_value = {
        "id": 3,
        "title": "@a",
        "source_url": "https://www.youtube.com/@a/videos",
    }

    result = await tools["nc_lucarne_subscribe_channel"](
        "https://www.youtube.com/@a", ctx
    )

    lucarne.add_channel.assert_awaited_once_with("https://www.youtube.com/@a")
    assert result.channel.id == 3


async def test_add_video_to_youtube_playlist_reads_as_conflict(tools, lucarne, ctx):
    lucarne.add_playlist_video.side_effect = _http_error(409)
    add = tools["nc_lucarne_add_video_to_playlist"]

    with pytest.raises(MCPError, match="conflict"):
        await add(2, "https://youtu.be/x", ctx)
