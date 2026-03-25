"""Client for Nextcloud Collectives app API (OCS)."""

import logging
from typing import Any

from nextcloud_mcp_server.client.base import BaseNextcloudClient

logger = logging.getLogger(__name__)

API_BASE = "/ocs/v2.php/apps/collectives/api/v1.0"


class OCSError(Exception):
    """Error returned in the OCS response envelope."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"OCS error {status_code}: {message}")


class CollectivesClient(BaseNextcloudClient):
    """Client for Nextcloud Collectives app operations."""

    app_name = "collectives"

    def _get_ocs_headers(self) -> dict[str, str]:
        """Get standard headers required for OCS API calls."""
        return {
            "OCS-APIRequest": "true",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _unwrap_ocs(self, response_json: dict[str, Any]) -> Any:
        """Unwrap OCS envelope, validating the status before returning data."""
        ocs = response_json["ocs"]
        meta = ocs.get("meta", {})
        status_code = meta.get("statuscode", 200)
        if status_code >= 400:
            message = meta.get("message", "OCS error")
            raise OCSError(status_code, message)
        return ocs["data"]

    # Collectives

    async def get_collectives(self) -> list[dict[str, Any]]:
        """List all collectives the user has access to."""
        response = await self._make_request(
            "GET", f"{API_BASE}/collectives", headers=self._get_ocs_headers()
        )
        data = self._unwrap_ocs(response.json())
        return data["collectives"]

    async def create_collective(
        self, name: str, emoji: str | None = None
    ) -> dict[str, Any]:
        """Create a new collective."""
        json_data: dict[str, Any] = {"name": name}
        if emoji is not None:
            json_data["emoji"] = emoji
        response = await self._make_request(
            "POST",
            f"{API_BASE}/collectives",
            json=json_data,
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["collective"]

    async def update_collective(
        self, collective_id: int, emoji: str | None = None
    ) -> dict[str, Any]:
        """Update a collective (emoji)."""
        json_data: dict[str, Any] = {}
        if emoji is not None:
            json_data["emoji"] = emoji
        response = await self._make_request(
            "PUT",
            f"{API_BASE}/collectives/{collective_id}",
            json=json_data,
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["collective"]

    # Pages

    async def get_pages(self, collective_id: int) -> list[dict[str, Any]]:
        """List all pages in a collective."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/pages",
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["pages"]

    async def get_page(self, collective_id: int, page_id: int) -> dict[str, Any]:
        """Get a single page's metadata."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}",
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["page"]

    async def create_page(
        self, collective_id: int, parent_id: int, title: str
    ) -> dict[str, Any]:
        """Create a new page under a parent page."""
        json_data = {"title": title}
        response = await self._make_request(
            "POST",
            f"{API_BASE}/collectives/{collective_id}/pages/{parent_id}",
            json=json_data,
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["page"]

    async def move_page(
        self,
        collective_id: int,
        page_id: int,
        parent_id: int | None = None,
        title: str | None = None,
        index: int = 0,
        copy: bool = False,
    ) -> dict[str, Any]:
        """Move or copy a page within a collective."""
        json_data: dict[str, Any] = {"index": index, "copy": copy}
        if parent_id is not None:
            json_data["parentId"] = parent_id
        if title is not None:
            json_data["title"] = title
        response = await self._make_request(
            "PUT",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}",
            json=json_data,
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["page"]

    async def trash_page(self, collective_id: int, page_id: int) -> None:
        """Move a page to trash (soft delete)."""
        await self._make_request(
            "DELETE",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}",
            headers=self._get_ocs_headers(),
        )

    async def set_page_emoji(
        self, collective_id: int, page_id: int, emoji: str | None
    ) -> dict[str, Any]:
        """Set or clear the emoji on a page."""
        json_data = {"emoji": emoji}
        response = await self._make_request(
            "PUT",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}/emoji",
            json=json_data,
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["page"]

    # Search

    async def search_pages(
        self, collective_id: int, query: str
    ) -> list[dict[str, Any]]:
        """Full-text search within a collective."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/search",
            params={"searchString": query},
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["pages"]

    # Tags

    async def get_tags(self, collective_id: int) -> list[dict[str, Any]]:
        """List all tags in a collective."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/tags",
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["tags"]

    async def create_tag(
        self, collective_id: int, name: str, color: str
    ) -> dict[str, Any]:
        """Create a new tag in a collective."""
        json_data = {"name": name, "color": color}
        response = await self._make_request(
            "POST",
            f"{API_BASE}/collectives/{collective_id}/tags",
            json=json_data,
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["tag"]

    async def assign_tag(self, collective_id: int, page_id: int, tag_id: int) -> None:
        """Assign a tag to a page."""
        await self._make_request(
            "PUT",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}/tags/{tag_id}",
            headers=self._get_ocs_headers(),
        )

    async def remove_tag(self, collective_id: int, page_id: int, tag_id: int) -> None:
        """Remove a tag from a page."""
        await self._make_request(
            "DELETE",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}/tags/{tag_id}",
            headers=self._get_ocs_headers(),
        )

    # Trash

    async def get_trashed_pages(self, collective_id: int) -> list[dict[str, Any]]:
        """List trashed pages in a collective."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/pages/trash",
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["pages"]

    async def restore_page(self, collective_id: int, page_id: int) -> dict[str, Any]:
        """Restore a page from trash."""
        response = await self._make_request(
            "PATCH",
            f"{API_BASE}/collectives/{collective_id}/pages/trash/{page_id}",
            headers=self._get_ocs_headers(),
        )
        data = self._unwrap_ocs(response.json())
        return data["page"]
