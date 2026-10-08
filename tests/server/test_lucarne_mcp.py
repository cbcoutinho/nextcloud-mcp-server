"""End-to-end coverage of the Lucarne MCP tools against a real stack.

Lucarne is an AppAPI external app, which needs AppAPI and a HaRP deploy daemon.
The default Docker stack has neither, so these tests skip themselves when
Lucarne is not reachable instead of failing.
"""

import json
import logging
import uuid

import httpx
import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


def _payload(result):
    assert result.is_error is False, f"MCP tool call failed: {result.content}"
    return json.loads(result.content[0].text)


@pytest.fixture(scope="module")
async def lucarne(nc_client: NextcloudClient):
    """The Lucarne client, or a skip when the app is not installed."""
    try:
        await nc_client.lucarne.get_catalogs()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        pytest.skip(f"Lucarne is not installed or not reachable: {e}")
    return nc_client.lucarne


async def _forget_channel(lucarne, channel_id: int) -> None:
    """Unsubscribe a test channel. The MCP tools deliberately cannot do this."""
    await lucarne._make_request(
        "DELETE",
        f"{lucarne.API_BASE}/channels/{channel_id}",
        json={"delete_videos": True},
    )


async def test_mcp_lucarne_catalog_round_trip(nc_mcp_client: ClientSession, lucarne):
    """Subscribe, file into a catalogue, take out, rename, delete."""
    suffix = uuid.uuid4().hex[:8]
    name = f"MCP Test {suffix}"
    catalog_id = None
    channel_ids: list[int] = []

    try:
        for n in range(2):
            subscribed = _payload(
                await nc_mcp_client.call_tool(
                    "nc_lucarne_subscribe_channel",
                    {"url": f"https://www.youtube.com/@mcp-test-{suffix}-{n}"},
                )
            )
            channel_ids.append(subscribed["channel"]["id"])

        created = _payload(
            await nc_mcp_client.call_tool("nc_lucarne_create_catalog", {"name": name})
        )
        catalog_id = created["catalog"]["id"]
        assert created["catalog"]["name"] == name

        # Filing is cumulative: the second call keeps the first channel.
        for channel_id in channel_ids:
            _payload(
                await nc_mcp_client.call_tool(
                    "nc_lucarne_add_channels_to_catalog",
                    {"catalog_id": catalog_id, "channel_ids": [channel_id]},
                )
            )
        # Verified through the direct client, not only the tool that wrote it.
        stored = await lucarne.get_catalog(catalog_id)
        assert sorted(stored["channel_ids"]) == sorted(channel_ids)

        listed = _payload(
            await nc_mcp_client.call_tool(
                "nc_lucarne_list_channels", {"catalog_id": catalog_id}
            )
        )
        assert {c["id"] for c in listed["results"]} == set(channel_ids)

        _payload(
            await nc_mcp_client.call_tool(
                "nc_lucarne_remove_channels_from_catalog",
                {"catalog_id": catalog_id, "channel_ids": [channel_ids[0]]},
            )
        )
        stored = await lucarne.get_catalog(catalog_id)
        assert stored["channel_ids"] == [channel_ids[1]]

        uncategorized = _payload(
            await nc_mcp_client.call_tool(
                "nc_lucarne_list_channels", {"uncategorized": True}
            )
        )
        assert channel_ids[0] in {c["id"] for c in uncategorized["results"]}

        renamed = _payload(
            await nc_mcp_client.call_tool(
                "nc_lucarne_update_catalog",
                {"catalog_id": catalog_id, "name": f"{name} renamed"},
            )
        )
        assert renamed["catalog"]["name"] == f"{name} renamed"

        duplicate = await nc_mcp_client.call_tool(
            "nc_lucarne_create_catalog", {"name": f"{name} RENAMED"}
        )
        assert duplicate.is_error is True
        assert "conflict" in duplicate.content[0].text
    finally:
        if catalog_id is not None:
            await lucarne.delete_catalog(catalog_id)
        for channel_id in channel_ids:
            await _forget_channel(lucarne, channel_id)


async def test_mcp_lucarne_playlist_round_trip(nc_mcp_client: ClientSession, lucarne):
    """Create, rename, add a video, delete a personal playlist."""
    title = f"MCP Test Playlist {uuid.uuid4().hex[:8]}"
    playlist_id = None

    try:
        created = _payload(
            await nc_mcp_client.call_tool(
                "nc_lucarne_create_playlist", {"title": title}
            )
        )
        playlist_id = created["playlist"]["id"]
        assert created["playlist"]["kind"] == "personal"

        renamed = _payload(
            await nc_mcp_client.call_tool(
                "nc_lucarne_update_playlist",
                {"playlist_id": playlist_id, "title": f"{title} renamed"},
            )
        )
        assert renamed["playlist"]["title"] == f"{title} renamed"

        # Lucarne inspects the video in the background, so only the hand-off
        # is asserted here, not the video joining the playlist.
        added = _payload(
            await nc_mcp_client.call_tool(
                "nc_lucarne_add_video_to_playlist",
                {
                    "playlist_id": playlist_id,
                    "url": "https://www.youtube.com/watch?v=jNQXAC9IJRE",
                },
            )
        )
        assert added["queued"] is True

        listed = _payload(
            await nc_mcp_client.call_tool("nc_lucarne_list_playlists", {})
        )
        assert playlist_id in {p["id"] for p in listed["results"]}

        deleted = _payload(
            await nc_mcp_client.call_tool(
                "nc_lucarne_delete_playlist", {"playlist_id": playlist_id}
            )
        )
        assert deleted["queued"] is True
        playlist_id = None

        # Lucarne hides a playlist the moment it is queued for deletion.
        listed = _payload(
            await nc_mcp_client.call_tool("nc_lucarne_list_playlists", {})
        )
        assert title not in {p["title"] for p in listed["results"]}
    finally:
        if playlist_id is not None:
            await lucarne.delete_playlist(playlist_id)


async def test_mcp_lucarne_unknown_ids_read_as_not_found(
    nc_mcp_client: ClientSession, lucarne
):
    result = await nc_mcp_client.call_tool(
        "nc_lucarne_get_catalog", {"catalog_id": 999999}
    )

    assert result.is_error is True
    assert "not found" in result.content[0].text
