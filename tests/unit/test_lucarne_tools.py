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


async def test_get_catalog_returns_the_channel_ids(tools, lucarne, ctx):
    """The single-catalogue read exposes which channels it contains."""
    lucarne.get_catalog.return_value = {"id": 4, "name": "Tech", "channel_ids": [1, 2]}

    result = await tools["nc_lucarne_get_catalog"](4, ctx)

    lucarne.get_catalog.assert_awaited_once_with(4)
    assert result.catalog.channel_ids == [1, 2]


async def test_get_catalog_not_found_reads_as_not_found(tools, lucarne, ctx):
    """A 404 reaches the model as a message it can act on."""
    lucarne.get_catalog.side_effect = _http_error(404)
    get = tools["nc_lucarne_get_catalog"]

    with pytest.raises(MCPError, match="not found"):
        await get(999, ctx)


async def test_update_catalog_renames(tools, lucarne, ctx):
    """Renaming passes the new name through and returns the stored catalogue."""
    lucarne.update_catalog.return_value = {"id": 4, "name": "Détente"}

    result = await tools["nc_lucarne_update_catalog"](4, "Détente", ctx)

    lucarne.update_catalog.assert_awaited_once_with(4, "Détente")
    assert result.catalog.name == "Détente"


async def test_delete_catalog_reports_the_deleted_id(tools, lucarne, ctx):
    """Deleting a catalogue answers with its ID."""
    result = await tools["nc_lucarne_delete_catalog"](4, ctx)

    lucarne.delete_catalog.assert_awaited_once_with(4)
    assert result.deleted_id == 4


async def test_list_playlists_counts_them(tools, lucarne, ctx):
    """The listing carries each playlist's video count."""
    lucarne.get_playlists.return_value = [
        {"id": 1, "title": "A", "kind": "personal", "video_count": 3},
        {"id": 2, "title": "B", "kind": "youtube", "video_count": 0},
    ]

    result = await tools["nc_lucarne_list_playlists"](ctx)

    assert result.total_count == 2
    assert result.results[0].video_count == 3


async def test_create_playlist_returns_the_personal_playlist(tools, lucarne, ctx):
    """A new playlist is a personal one."""
    lucarne.create_playlist.return_value = {
        "id": 9,
        "title": "Soir",
        "kind": "personal",
    }

    result = await tools["nc_lucarne_create_playlist"]("Soir", ctx)

    lucarne.create_playlist.assert_awaited_once_with("Soir")
    assert result.playlist.kind == "personal"


async def test_update_playlist_shows_the_title_actually_stored(tools, lucarne, ctx):
    """An imported playlist keeps its title, and the response is how to tell."""
    lucarne.update_playlist.return_value = {
        "id": 2,
        "title": "Original",
        "kind": "youtube",
    }

    result = await tools["nc_lucarne_update_playlist"](2, "Nouveau", ctx)

    assert result.playlist.title == "Original"


async def test_add_video_to_playlist_reports_it_is_queued(tools, lucarne, ctx):
    """Lucarne inspects the video later, so the answer is a hand-off."""
    lucarne.add_playlist_video.return_value = {"queued": True}

    result = await tools["nc_lucarne_add_video_to_playlist"](
        2, "https://youtu.be/x", ctx
    )

    lucarne.add_playlist_video.assert_awaited_once_with(2, "https://youtu.be/x")
    assert result.queued is True


@pytest.mark.parametrize(
    ("tool", "parameter", "limit"),
    [
        ("nc_lucarne_create_catalog", "name", 100),
        ("nc_lucarne_update_catalog", "name", 100),
        ("nc_lucarne_create_playlist", "title", 255),
        ("nc_lucarne_update_playlist", "title", 255),
        ("nc_lucarne_subscribe_channel", "url", 2048),
        ("nc_lucarne_add_video_to_playlist", "url", 2048),
    ],
)
def test_text_parameters_carry_lucarnes_length_limits(tool, parameter, limit):
    """An over-long value is refused up front instead of coming back as a 422."""
    mcp = MCPServer(name="test-lucarne-schema")
    configure_lucarne_tools(mcp)
    schema = {t.name: t for t in mcp._tool_manager.list_tools()}[tool].parameters

    assert schema["properties"][parameter]["maxLength"] == limit
