"""MCP tools for the Lucarne app (catalogues, channel filing and playlists).

Lucarne is an AppAPI external application, reached through the AppAPI proxy.
Its API answers a missing resource with 404 and a clash with 409, so the
translation to ``MCPError`` lives in one context manager rather than a
try/except per tool.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from httpx import HTTPStatusError, RequestError
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations
from pydantic import Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.lucarne import (
    AddLucarnePlaylistVideoResponse,
    DeleteLucarneCatalogResponse,
    DeleteLucarnePlaylistResponse,
    ListLucarneCatalogsResponse,
    ListLucarneChannelsResponse,
    ListLucarnePlaylistsResponse,
    LucarneCatalog,
    LucarneCatalogResponse,
    LucarneChannel,
    LucarneChannelResponse,
    LucarnePlaylist,
    LucarnePlaylistResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)

# Lucarne's own limits, applied here so an over-long value is refused before the
# request instead of coming back as a 422.
CatalogName = Annotated[str, Field(min_length=1, max_length=100)]
PlaylistTitle = Annotated[str, Field(min_length=1, max_length=255)]
YouTubeUrl = Annotated[str, Field(min_length=1, max_length=2048)]


@contextmanager
def _lucarne_errors(action: str) -> Iterator[None]:
    """Re-raise a Lucarne failure as the message the model should act on."""
    try:
        yield
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error {action}: {e}")
    except HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            detail = "not found (or the Lucarne app is not installed or enabled)"
        elif status == 409:
            detail = "conflict (a catalogue with this name may already exist)"
        elif status in (400, 422):
            detail = "rejected as invalid (check the IDs and names)"
        else:
            detail = f"server error ({status})"
        raise MCPError(code=-1, message=f"Failed {action}: {detail}")


def configure_lucarne_tools(mcp: MCPServer):
    """Configure Lucarne app MCP tools."""

    # --- Channels ---

    @mcp.tool(
        title="List Lucarne Channels",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.read")
    @instrument_tool
    async def nc_lucarne_list_channels(
        ctx: Context,
        catalog_id: int | None = None,
        uncategorized: bool = False,
    ) -> ListLucarneChannelsResponse:
        """List the Lucarne channel subscriptions (requires lucarne.read scope).

        Args:
            catalog_id: Only the channels filed in this catalogue
            uncategorized: Only the channels that are in no catalogue
        """
        client = await get_client(ctx)
        with _lucarne_errors("listing channels"):
            data = await client.lucarne.get_channels(catalog_id, uncategorized)
        channels = [LucarneChannel(**c) for c in data]
        return ListLucarneChannelsResponse(results=channels, total_count=len(channels))

    @mcp.tool(
        title="Subscribe to Lucarne Channel",
        # Create-or-get: subscribing again to the same channel returns the
        # existing subscription instead of making a second one.
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_subscribe_channel(
        url: YouTubeUrl, ctx: Context
    ) -> LucarneChannelResponse:
        """Subscribe to a YouTube channel in Lucarne (requires lucarne.write scope).

        Lucarne finishes setting the channel up in the background, so its title
        and videos may take a moment to appear.

        Args:
            url: YouTube channel URL, for example https://www.youtube.com/@Fireship
        """
        client = await get_client(ctx)
        with _lucarne_errors(f"subscribing to channel {url}"):
            data = await client.lucarne.add_channel(url)
        return LucarneChannelResponse(channel=LucarneChannel(**data))

    # --- Catalogues ---

    @mcp.tool(
        title="List Lucarne Catalogues",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.read")
    @instrument_tool
    async def nc_lucarne_list_catalogs(ctx: Context) -> ListLucarneCatalogsResponse:
        """List the Lucarne catalogues with their channel count (requires lucarne.read scope)."""
        client = await get_client(ctx)
        with _lucarne_errors("listing catalogues"):
            data = await client.lucarne.get_catalogs()
        catalogs = [LucarneCatalog(**c) for c in data]
        return ListLucarneCatalogsResponse(results=catalogs, total_count=len(catalogs))

    @mcp.tool(
        title="Get Lucarne Catalogue",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.read")
    @instrument_tool
    async def nc_lucarne_get_catalog(
        catalog_id: int, ctx: Context
    ) -> LucarneCatalogResponse:
        """Get a Lucarne catalogue with the IDs of its channels (requires lucarne.read scope)."""
        client = await get_client(ctx)
        with _lucarne_errors(f"getting catalogue {catalog_id}"):
            data = await client.lucarne.get_catalog(catalog_id)
        return LucarneCatalogResponse(catalog=LucarneCatalog(**data))

    @mcp.tool(
        title="Create Lucarne Catalogue",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_create_catalog(
        name: CatalogName, ctx: Context
    ) -> LucarneCatalogResponse:
        """Create an empty Lucarne catalogue (requires lucarne.write scope).

        Names are unique per user, ignoring case, so a duplicate is refused.
        """
        client = await get_client(ctx)
        with _lucarne_errors(f"creating catalogue '{name}'"):
            data = await client.lucarne.create_catalog(name)
        return LucarneCatalogResponse(catalog=LucarneCatalog(**data))

    @mcp.tool(
        title="Rename Lucarne Catalogue",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_update_catalog(
        catalog_id: int, name: CatalogName, ctx: Context
    ) -> LucarneCatalogResponse:
        """Rename a Lucarne catalogue (requires lucarne.write scope)."""
        client = await get_client(ctx)
        with _lucarne_errors(f"renaming catalogue {catalog_id}"):
            data = await client.lucarne.update_catalog(catalog_id, name)
        return LucarneCatalogResponse(catalog=LucarneCatalog(**data))

    @mcp.tool(
        title="Delete Lucarne Catalogue",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_delete_catalog(
        catalog_id: int, ctx: Context
    ) -> DeleteLucarneCatalogResponse:
        """Delete a Lucarne catalogue (requires lucarne.write scope).

        The channels it contained stay subscribed and become uncatalogued.
        """
        client = await get_client(ctx)
        with _lucarne_errors(f"deleting catalogue {catalog_id}"):
            await client.lucarne.delete_catalog(catalog_id)
        return DeleteLucarneCatalogResponse(deleted_id=catalog_id)

    @mcp.tool(
        title="Add Channels to Lucarne Catalogue",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_add_channels_to_catalog(
        catalog_id: int, channel_ids: list[int], ctx: Context
    ) -> LucarneCatalogResponse:
        """File channels into a Lucarne catalogue (requires lucarne.write scope).

        The channels already in the catalogue are kept. Get channel IDs from
        nc_lucarne_list_channels.

        Lucarne can only replace a catalogue's whole channel set, so this reads
        the catalogue and writes it back. Two edits to the same catalogue at the
        same moment can overwrite each other.

        Args:
            catalog_id: Catalogue to file the channels into
            channel_ids: IDs of the channels to add
        """
        client = await get_client(ctx)
        with _lucarne_errors(f"filing channels into catalogue {catalog_id}"):
            current = await client.lucarne.get_catalog(catalog_id)
            merged = sorted({*(current.get("channel_ids") or []), *channel_ids})
            data = await client.lucarne.replace_catalog_channels(catalog_id, merged)
        return LucarneCatalogResponse(catalog=LucarneCatalog(**data))

    @mcp.tool(
        title="Remove Channels from Lucarne Catalogue",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_remove_channels_from_catalog(
        catalog_id: int, channel_ids: list[int], ctx: Context
    ) -> LucarneCatalogResponse:
        """Take channels out of a Lucarne catalogue (requires lucarne.write scope).

        The channels stay subscribed, they simply stop being in this catalogue.

        Lucarne can only replace a catalogue's whole channel set, so this reads
        the catalogue and writes it back. Two edits to the same catalogue at the
        same moment can overwrite each other.

        Args:
            catalog_id: Catalogue to take the channels out of
            channel_ids: IDs of the channels to remove
        """
        client = await get_client(ctx)
        with _lucarne_errors(f"removing channels from catalogue {catalog_id}"):
            current = await client.lucarne.get_catalog(catalog_id)
            removed = set(channel_ids)
            kept = [
                cid for cid in current.get("channel_ids") or [] if cid not in removed
            ]
            data = await client.lucarne.replace_catalog_channels(catalog_id, kept)
        return LucarneCatalogResponse(catalog=LucarneCatalog(**data))

    # --- Playlists ---

    @mcp.tool(
        title="List Lucarne Playlists",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.read")
    @instrument_tool
    async def nc_lucarne_list_playlists(ctx: Context) -> ListLucarnePlaylistsResponse:
        """List the Lucarne playlists with their video count (requires lucarne.read scope)."""
        client = await get_client(ctx)
        with _lucarne_errors("listing playlists"):
            data = await client.lucarne.get_playlists()
        playlists = [LucarnePlaylist(**p) for p in data]
        return ListLucarnePlaylistsResponse(
            results=playlists, total_count=len(playlists)
        )

    @mcp.tool(
        title="Create Lucarne Playlist",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_create_playlist(
        title: PlaylistTitle, ctx: Context
    ) -> LucarnePlaylistResponse:
        """Create an empty personal Lucarne playlist (requires lucarne.write scope)."""
        client = await get_client(ctx)
        with _lucarne_errors(f"creating playlist '{title}'"):
            data = await client.lucarne.create_playlist(title)
        return LucarnePlaylistResponse(playlist=LucarnePlaylist(**data))

    @mcp.tool(
        title="Rename Lucarne Playlist",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_update_playlist(
        playlist_id: int, title: PlaylistTitle, ctx: Context
    ) -> LucarnePlaylistResponse:
        """Rename a Lucarne playlist (requires lucarne.write scope).

        A playlist imported from YouTube keeps its title: the response shows the
        title actually stored, which is how to tell the rename was ignored.
        """
        client = await get_client(ctx)
        with _lucarne_errors(f"renaming playlist {playlist_id}"):
            data = await client.lucarne.update_playlist(playlist_id, title)
        return LucarnePlaylistResponse(playlist=LucarnePlaylist(**data))

    @mcp.tool(
        title="Delete Lucarne Playlist",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_delete_playlist(
        playlist_id: int, ctx: Context, delete_videos: bool = False
    ) -> DeleteLucarnePlaylistResponse:
        """Delete a Lucarne playlist (requires lucarne.write scope).

        Lucarne deletes asynchronously, so the playlist may linger briefly.

        Args:
            playlist_id: Playlist to delete
            delete_videos: Also delete the videos that belong to no other
                playlist. Off by default: the videos are kept.
        """
        client = await get_client(ctx)
        with _lucarne_errors(f"deleting playlist {playlist_id}"):
            data = await client.lucarne.delete_playlist(playlist_id, delete_videos)
        # Lucarne answers 202 with {"queued": true}. The fallback only covers a
        # body without the key, since the 202 itself means the work was queued.
        return DeleteLucarnePlaylistResponse(
            playlist_id=playlist_id, queued=bool(data.get("queued", True))
        )

    @mcp.tool(
        title="Add Video to Lucarne Playlist",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("lucarne.write")
    @instrument_tool
    async def nc_lucarne_add_video_to_playlist(
        playlist_id: int, url: YouTubeUrl, ctx: Context
    ) -> AddLucarnePlaylistVideoResponse:
        """Add a YouTube video to a personal Lucarne playlist (requires lucarne.write scope).

        Lucarne inspects the video in the background, so it joins the playlist a
        moment later. A playlist imported from YouTube cannot be changed this way.

        Args:
            playlist_id: Personal playlist to add the video to
            url: YouTube video URL, for example https://www.youtube.com/watch?v=...
        """
        client = await get_client(ctx)
        with _lucarne_errors(f"adding a video to playlist {playlist_id}"):
            data = await client.lucarne.add_playlist_video(playlist_id, url)
        # {"queued": false} means the video was already known and attached at
        # once. The fallback only covers a body without the key.
        return AddLucarnePlaylistVideoResponse(
            playlist_id=playlist_id, queued=bool(data.get("queued", True))
        )
