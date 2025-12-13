"""Client for Nextcloud News app operations."""

import logging
from enum import IntEnum
from typing import Any

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


class NewsItemType(IntEnum):
    """Type constants for News API item queries."""

    FEED = 0  # Single feed
    FOLDER = 1  # Folder and its feeds
    STARRED = 2  # All starred items
    ALL = 3  # All items


class NewsClient(BaseNextcloudClient):
    """Client for Nextcloud News app operations."""

    app_name = "news"
    API_BASE = "/apps/news/api/v1-3"

    # --- Folders ---

    async def get_folders(self) -> list[dict[str, Any]]:
        """Get all folders."""
        response = await self._make_request("GET", f"{self.API_BASE}/folders")
        return response.json().get("folders", [])

    async def create_folder(self, name: str) -> dict[str, Any]:
        """Create a new folder.

        Args:
            name: Folder name

        Returns:
            Created folder data

        Raises:
            HTTPStatusError: 409 if folder name already exists,
                            422 if name is empty
        """
        response = await self._make_request(
            "POST", f"{self.API_BASE}/folders", json={"name": name}
        )
        folders = response.json().get("folders", [])
        return folders[0] if folders else {}

    async def rename_folder(self, folder_id: int, name: str) -> None:
        """Rename a folder.

        Args:
            folder_id: Folder ID
            name: New folder name

        Raises:
            HTTPStatusError: 404 if folder not found, 409 if name exists
        """
        await self._make_request(
            "PUT", f"{self.API_BASE}/folders/{folder_id}", json={"name": name}
        )

    async def delete_folder(self, folder_id: int) -> None:
        """Delete a folder and all its feeds/items.

        Args:
            folder_id: Folder ID

        Raises:
            HTTPStatusError: 404 if folder not found
        """
        await self._make_request("DELETE", f"{self.API_BASE}/folders/{folder_id}")

    async def mark_folder_read(self, folder_id: int, newest_item_id: int) -> None:
        """Mark all items in a folder as read.

        Args:
            folder_id: Folder ID
            newest_item_id: ID of newest item to mark read (prevents marking
                           items user hasn't seen yet)

        Raises:
            HTTPStatusError: 404 if folder not found
        """
        await self._make_request(
            "POST",
            f"{self.API_BASE}/folders/{folder_id}/read",
            json={"newestItemId": newest_item_id},
        )

    # --- Feeds ---

    async def get_feeds(self) -> dict[str, Any]:
        """Get all feeds with metadata.

        Returns:
            Dict with keys:
                - feeds: List of feed objects
                - starredCount: Number of starred items
                - newestItemId: ID of newest item (omitted if no items)
        """
        response = await self._make_request("GET", f"{self.API_BASE}/feeds")
        return response.json()

    async def create_feed(
        self, url: str, folder_id: int | None = None
    ) -> dict[str, Any]:
        """Subscribe to a new feed.

        Args:
            url: Feed URL
            folder_id: Optional folder ID (None for root)

        Returns:
            Created feed data

        Raises:
            HTTPStatusError: 409 if feed already exists, 422 if URL is invalid
        """
        body: dict[str, Any] = {"url": url}
        if folder_id is not None:
            body["folderId"] = folder_id
        response = await self._make_request("POST", f"{self.API_BASE}/feeds", json=body)
        data = response.json()
        feeds = data.get("feeds", [])
        return feeds[0] if feeds else {}

    async def delete_feed(self, feed_id: int) -> None:
        """Unsubscribe from a feed (deletes all items).

        Args:
            feed_id: Feed ID

        Raises:
            HTTPStatusError: 404 if feed not found
        """
        await self._make_request("DELETE", f"{self.API_BASE}/feeds/{feed_id}")

    async def move_feed(self, feed_id: int, folder_id: int | None) -> None:
        """Move a feed to a different folder.

        Args:
            feed_id: Feed ID
            folder_id: Target folder ID (None for root)

        Raises:
            HTTPStatusError: 404 if feed not found
        """
        await self._make_request(
            "POST",
            f"{self.API_BASE}/feeds/{feed_id}/move",
            json={"folderId": folder_id},
        )

    async def rename_feed(self, feed_id: int, title: str) -> None:
        """Rename a feed.

        Args:
            feed_id: Feed ID
            title: New feed title

        Raises:
            HTTPStatusError: 404 if feed not found
        """
        await self._make_request(
            "POST",
            f"{self.API_BASE}/feeds/{feed_id}/rename",
            json={"feedTitle": title},
        )

    async def mark_feed_read(self, feed_id: int, newest_item_id: int) -> None:
        """Mark all items in a feed as read.

        Args:
            feed_id: Feed ID
            newest_item_id: ID of newest item to mark read

        Raises:
            HTTPStatusError: 404 if feed not found
        """
        await self._make_request(
            "POST",
            f"{self.API_BASE}/feeds/{feed_id}/read",
            json={"newestItemId": newest_item_id},
        )

    # --- Items ---

    async def get_items(
        self,
        batch_size: int = 50,
        offset: int = 0,
        type_: int = NewsItemType.ALL,
        id_: int = 0,
        get_read: bool = True,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Get items (articles) with filtering.

        Args:
            batch_size: Number of items to return (-1 for all)
            offset: Item ID to start after (for pagination)
            type_: Item type filter (NewsItemType)
            id_: Feed/folder ID (ignored for STARRED/ALL types)
            get_read: Include read items
            oldest_first: Sort oldest first instead of newest

        Returns:
            List of item objects
        """
        params: dict[str, Any] = {
            "batchSize": batch_size,
            "offset": offset,
            "type": type_,
            "id": id_,
            "getRead": str(get_read).lower(),
            "oldestFirst": str(oldest_first).lower(),
        }
        response = await self._make_request(
            "GET", f"{self.API_BASE}/items", params=params
        )
        return response.json().get("items", [])

    async def get_item(self, item_id: int) -> dict[str, Any]:
        """Get a specific item by ID.

        Note: The News API doesn't have a direct single-item endpoint,
        so we fetch all items and filter. For efficiency, consider
        caching or using get_items with specific feed if known.

        Args:
            item_id: Item ID

        Returns:
            Item data

        Raises:
            ValueError: If item not found
        """
        # Fetch all items and find the one we need
        # This is inefficient but the API doesn't provide a direct endpoint
        items = await self.get_items(batch_size=-1, get_read=True)
        for item in items:
            if item.get("id") == item_id:
                return item
        raise ValueError(f"Item {item_id} not found")

    async def get_updated_items(
        self,
        last_modified: int,
        type_: int = NewsItemType.ALL,
        id_: int = 0,
    ) -> list[dict[str, Any]]:
        """Get items modified since a timestamp (for delta sync).

        Args:
            last_modified: Unix timestamp (seconds or microseconds)
            type_: Item type filter
            id_: Feed/folder ID

        Returns:
            List of modified items (includes deleted items)
        """
        params: dict[str, Any] = {
            "lastModified": last_modified,
            "type": type_,
            "id": id_,
        }
        response = await self._make_request(
            "GET", f"{self.API_BASE}/items/updated", params=params
        )
        return response.json().get("items", [])

    async def mark_item_read(self, item_id: int) -> None:
        """Mark a single item as read.

        Args:
            item_id: Item ID

        Raises:
            HTTPStatusError: 404 if item not found
        """
        await self._make_request("POST", f"{self.API_BASE}/items/{item_id}/read")

    async def mark_item_unread(self, item_id: int) -> None:
        """Mark a single item as unread.

        Args:
            item_id: Item ID

        Raises:
            HTTPStatusError: 404 if item not found
        """
        await self._make_request("POST", f"{self.API_BASE}/items/{item_id}/unread")

    async def star_item(self, item_id: int) -> None:
        """Star (favorite) a single item.

        Args:
            item_id: Item ID

        Raises:
            HTTPStatusError: 404 if item not found
        """
        await self._make_request("POST", f"{self.API_BASE}/items/{item_id}/star")

    async def unstar_item(self, item_id: int) -> None:
        """Unstar a single item.

        Args:
            item_id: Item ID

        Raises:
            HTTPStatusError: 404 if item not found
        """
        await self._make_request("POST", f"{self.API_BASE}/items/{item_id}/unstar")

    async def mark_items_read(self, item_ids: list[int]) -> None:
        """Mark multiple items as read.

        Args:
            item_ids: List of item IDs
        """
        await self._make_request(
            "POST", f"{self.API_BASE}/items/read/multiple", json={"itemIds": item_ids}
        )

    async def mark_items_unread(self, item_ids: list[int]) -> None:
        """Mark multiple items as unread.

        Args:
            item_ids: List of item IDs
        """
        await self._make_request(
            "POST",
            f"{self.API_BASE}/items/unread/multiple",
            json={"itemIds": item_ids},
        )

    async def star_items(self, item_ids: list[int]) -> None:
        """Star multiple items.

        Args:
            item_ids: List of item IDs
        """
        await self._make_request(
            "POST", f"{self.API_BASE}/items/star/multiple", json={"itemIds": item_ids}
        )

    async def unstar_items(self, item_ids: list[int]) -> None:
        """Unstar multiple items.

        Args:
            item_ids: List of item IDs
        """
        await self._make_request(
            "POST",
            f"{self.API_BASE}/items/unstar/multiple",
            json={"itemIds": item_ids},
        )

    async def mark_all_read(self, newest_item_id: int) -> None:
        """Mark all items as read.

        Args:
            newest_item_id: ID of newest item to mark read
        """
        await self._make_request(
            "POST", f"{self.API_BASE}/items/read", json={"newestItemId": newest_item_id}
        )

    # --- Status ---

    async def get_status(self) -> dict[str, Any]:
        """Get News app status and configuration.

        Returns:
            Dict with version and warnings
        """
        response = await self._make_request("GET", f"{self.API_BASE}/status")
        return response.json()

    async def get_version(self) -> str:
        """Get News app version.

        Returns:
            Version string (e.g., "25.0.0")
        """
        response = await self._make_request("GET", f"{self.API_BASE}/version")
        return response.json().get("version", "")
