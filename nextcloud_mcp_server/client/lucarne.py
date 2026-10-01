"""Client for the Lucarne app (a Nextcloud AppAPI external application).

Lucarne runs in its own container, so its REST API is reached through the
AppAPI proxy, which authenticates the request as the Nextcloud user and
forwards it to the ExApp.
"""

import logging
from typing import Any

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


class LucarneClient(BaseNextcloudClient):
    """Client for the Lucarne catalogue and playlist API."""

    app_name = "lucarne"
    API_BASE = "/apps/app_api/proxy/lucarne/api"

    # --- Channels (subscriptions) ---

    async def get_channels(
        self, catalog_id: int | None = None, uncategorized: bool = False
    ) -> list[dict[str, Any]]:
        """List the user's channel subscriptions.

        Args:
            catalog_id: Only channels in this catalogue
            uncategorized: Only channels that belong to no catalogue
        """
        params: dict[str, Any] = {}
        if catalog_id is not None:
            params["catalog_id"] = catalog_id
        if uncategorized:
            params["uncategorized"] = "true"
        response = await self._make_request(
            "GET", f"{self.API_BASE}/channels", params=params
        )
        return response.json()

    # --- Catalogues ---

    async def get_catalogs(self) -> list[dict[str, Any]]:
        """List the user's catalogues, each with its channel count."""
        response = await self._make_request("GET", f"{self.API_BASE}/catalogs")
        return response.json()

    async def get_catalog(self, catalog_id: int) -> dict[str, Any]:
        """Get a catalogue, including the IDs of the channels it contains.

        Raises:
            HTTPStatusError: 404 if the catalogue does not exist
        """
        response = await self._make_request(
            "GET", f"{self.API_BASE}/catalogs/{catalog_id}"
        )
        return response.json()

    async def create_catalog(self, name: str) -> dict[str, Any]:
        """Create a catalogue.

        Raises:
            HTTPStatusError: 409 if a catalogue with this name already exists
        """
        response = await self._make_request(
            "POST", f"{self.API_BASE}/catalogs", json={"name": name}
        )
        return response.json()

    async def update_catalog(self, catalog_id: int, name: str) -> dict[str, Any]:
        """Rename a catalogue.

        Raises:
            HTTPStatusError: 404 if not found, 409 if the name is taken
        """
        response = await self._make_request(
            "PUT", f"{self.API_BASE}/catalogs/{catalog_id}", json={"name": name}
        )
        return response.json()

    async def replace_catalog_channels(
        self, catalog_id: int, channel_ids: list[int]
    ) -> dict[str, Any]:
        """Replace the whole set of channels in a catalogue.

        Raises:
            HTTPStatusError: 404 if the catalogue is not found, 4xx if a
                channel does not belong to the user
        """
        response = await self._make_request(
            "PUT",
            f"{self.API_BASE}/catalogs/{catalog_id}/channels",
            json={"channel_ids": channel_ids},
        )
        return response.json()

    async def delete_catalog(self, catalog_id: int) -> None:
        """Delete a catalogue. Its channels stay subscribed, uncatalogued.

        Raises:
            HTTPStatusError: 404 if the catalogue is not found
        """
        await self._make_request("DELETE", f"{self.API_BASE}/catalogs/{catalog_id}")

    # --- Playlists ---

    async def get_playlists(self) -> list[dict[str, Any]]:
        """List the user's playlists, each with its video count."""
        response = await self._make_request("GET", f"{self.API_BASE}/playlists")
        return response.json()

    async def create_playlist(self, title: str) -> dict[str, Any]:
        """Create a personal playlist."""
        response = await self._make_request(
            "POST", f"{self.API_BASE}/playlists", json={"title": title}
        )
        return response.json()

    async def update_playlist(self, playlist_id: int, title: str) -> dict[str, Any]:
        """Rename a playlist.

        Lucarne keeps a YouTube-imported playlist's title and silently ignores
        the new one; the returned playlist shows the title actually stored.

        Raises:
            HTTPStatusError: 404 if the playlist is not found
        """
        response = await self._make_request(
            "PUT", f"{self.API_BASE}/playlists/{playlist_id}", json={"title": title}
        )
        return response.json()

    async def delete_playlist(
        self, playlist_id: int, delete_videos: bool = False
    ) -> dict[str, Any]:
        """Queue a playlist for deletion (Lucarne deletes it asynchronously).

        Args:
            playlist_id: Playlist ID
            delete_videos: Also delete the videos that were only in this playlist

        Raises:
            HTTPStatusError: 404 if the playlist is not found
        """
        response = await self._make_request(
            "DELETE",
            f"{self.API_BASE}/playlists/{playlist_id}",
            json={"delete_videos": delete_videos},
        )
        return response.json()
