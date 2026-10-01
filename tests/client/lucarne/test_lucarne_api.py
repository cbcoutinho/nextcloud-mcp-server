"""Mocked unit tests for the Lucarne client.

Lucarne is an AppAPI ExApp, so every call goes through the AppAPI proxy path.
These pin that path, the request bodies and the query parameters, none of which
the tool layer can see.
"""

import httpx
import pytest

from nextcloud_mcp_server.client.lucarne import LucarneClient
from tests.client.conftest import create_mock_response

pytestmark = pytest.mark.unit

API = "/apps/app_api/proxy/lucarne/api"


def _client(mocker, json_data=None):
    mock_make_request = mocker.patch.object(
        LucarneClient,
        "_make_request",
        return_value=create_mock_response(json_data=json_data),
    )
    client = LucarneClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    return client, mock_make_request


async def test_get_channels_without_filter_sends_no_params(mocker):
    client, request = _client(mocker, [{"id": 1, "title": "A"}])

    channels = await client.get_channels()

    assert channels == [{"id": 1, "title": "A"}]
    request.assert_called_once_with("GET", f"{API}/channels", params={})


async def test_get_channels_filters(mocker):
    client, request = _client(mocker, [])

    await client.get_channels(catalog_id=4, uncategorized=True)

    request.assert_called_once_with(
        "GET", f"{API}/channels", params={"catalog_id": 4, "uncategorized": "true"}
    )


async def test_get_catalogs(mocker):
    client, request = _client(mocker, [{"id": 1, "name": "Tech"}])

    assert await client.get_catalogs() == [{"id": 1, "name": "Tech"}]
    request.assert_called_once_with("GET", f"{API}/catalogs")


async def test_get_catalog(mocker):
    client, request = _client(mocker, {"id": 2, "name": "X", "channel_ids": [1]})

    catalog = await client.get_catalog(2)

    assert catalog["channel_ids"] == [1]
    request.assert_called_once_with("GET", f"{API}/catalogs/2")


async def test_create_catalog(mocker):
    client, request = _client(mocker, {"id": 3, "name": "Music"})

    await client.create_catalog("Music")

    request.assert_called_once_with("POST", f"{API}/catalogs", json={"name": "Music"})


async def test_update_catalog(mocker):
    client, request = _client(mocker, {"id": 3, "name": "Jazz"})

    await client.update_catalog(3, "Jazz")

    request.assert_called_once_with("PUT", f"{API}/catalogs/3", json={"name": "Jazz"})


async def test_replace_catalog_channels(mocker):
    client, request = _client(mocker, {"id": 3, "name": "Jazz", "channel_ids": [1, 2]})

    await client.replace_catalog_channels(3, [1, 2])

    request.assert_called_once_with(
        "PUT", f"{API}/catalogs/3/channels", json={"channel_ids": [1, 2]}
    )


async def test_delete_catalog(mocker):
    client, request = _client(mocker, {"deleted": True})

    await client.delete_catalog(3)

    request.assert_called_once_with("DELETE", f"{API}/catalogs/3")


async def test_get_playlists(mocker):
    client, request = _client(mocker, [{"id": 1, "title": "P", "kind": "personal"}])

    assert len(await client.get_playlists()) == 1
    request.assert_called_once_with("GET", f"{API}/playlists")


async def test_create_playlist(mocker):
    client, request = _client(mocker, {"id": 1, "title": "P", "kind": "personal"})

    await client.create_playlist("P")

    request.assert_called_once_with("POST", f"{API}/playlists", json={"title": "P"})


async def test_update_playlist(mocker):
    client, request = _client(mocker, {"id": 1, "title": "Q", "kind": "personal"})

    await client.update_playlist(1, "Q")

    request.assert_called_once_with("PUT", f"{API}/playlists/1", json={"title": "Q"})


@pytest.mark.parametrize("delete_videos", [False, True])
async def test_delete_playlist_sends_the_body_lucarne_requires(mocker, delete_videos):
    """Lucarne reads ``delete_videos`` from the DELETE body, so it must be sent."""
    client, request = _client(mocker, {"queued": True})

    result = await client.delete_playlist(1, delete_videos=delete_videos)

    assert result == {"queued": True}
    request.assert_called_once_with(
        "DELETE", f"{API}/playlists/1", json={"delete_videos": delete_videos}
    )


async def test_add_channel(mocker):
    client, request = _client(mocker, {"id": 1, "title": "@a"})

    await client.add_channel("https://www.youtube.com/@a")

    request.assert_called_once_with(
        "POST", f"{API}/channels", json={"url": "https://www.youtube.com/@a"}
    )


async def test_add_playlist_video(mocker):
    client, request = _client(mocker, {"queued": True})

    result = await client.add_playlist_video(2, "https://youtu.be/x")

    assert result == {"queued": True}
    request.assert_called_once_with(
        "POST", f"{API}/playlists/2/videos", json={"url": "https://youtu.be/x"}
    )
