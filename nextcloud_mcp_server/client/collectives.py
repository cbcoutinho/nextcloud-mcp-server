"""Client for Nextcloud Collectives app API (OCS)."""

import logging
from typing import Any

from nextcloud_mcp_server.client.base import BaseNextcloudClient
from nextcloud_mcp_server.client.ocs import (
    OCS_REQUEST_HEADERS,
    describe_ocs_failure,
    parse_ocs_envelope,
)

logger = logging.getLogger(__name__)

API_BASE = "/ocs/v2.php/apps/collectives/api/v1.0"

_UNSET = object()
"""Sentinel to distinguish 'not provided' from an explicit None."""


class OCSError(Exception):
    """Error returned in the OCS response envelope."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        # No status prefix here: server-side failures arrive already described
        # by ``describe_ocs_failure``, which names the code (and, for 997, what
        # it actually means). Re-prefixing produced "OCS error 404: OCS API
        # error (code 404): ..." in logs and tracebacks. ``status_code`` stays
        # available for the internally-raised cases that carry a bare message.
        super().__init__(message)


class CollectivesClient(BaseNextcloudClient):
    """Client for Nextcloud Collectives app operations."""

    app_name = "collectives"

    # Sourced from the shared OCS module so a new call site cannot pick up a
    # drifted copy of the header set -- omitting OCS-APIRequest is what makes
    # Nextcloud answer 997, the failure this module exists to make legible.
    _OCS_HEADERS: dict[str, str] = dict(OCS_REQUEST_HEADERS)

    _OCS_HEADERS_JSON: dict[str, str] = {
        **OCS_REQUEST_HEADERS,
        "Content-Type": "application/json",
    }

    def _unwrap_ocs(self, response_json: dict[str, Any]) -> Any:
        """Unwrap OCS envelope, validating the status before returning data.

        Raises ``OCSError``, which ``server/collectives`` catches in a dozen
        places. Parsing and failure wording come from :mod:`.ocs`; the
        ``>= 400`` rule stays local because it is looser than the documented
        100/200 success codes and retightening it here would be a behaviour
        change smuggled into a refactor.
        """
        envelope = parse_ocs_envelope(response_json)
        if envelope.status_code >= 400:
            raise OCSError(
                envelope.status_code,
                describe_ocs_failure(envelope.status_code, envelope.message),
            )
        if not envelope.has_data:
            raise OCSError(500, "OCS response missing 'data' field")
        return envelope.data

    # Collectives

    async def get_collectives(self) -> list[dict[str, Any]]:
        """List all collectives the user has access to."""
        response = await self._make_request(
            "GET", f"{API_BASE}/collectives", headers=self._OCS_HEADERS
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
            headers=self._OCS_HEADERS_JSON,
        )
        data = self._unwrap_ocs(response.json())
        return data["collective"]

    async def update_collective(
        self, collective_id: int, emoji: str | None | object = _UNSET
    ) -> dict[str, Any]:
        """Update a collective (emoji).

        Pass emoji=None to clear the emoji. Omit emoji entirely to leave
        it unchanged.

        Raises:
            ValueError: If no fields are provided to update.
        """
        json_data: dict[str, Any] = {}
        if emoji is not _UNSET:
            json_data["emoji"] = emoji
        if not json_data:
            raise ValueError("At least one field must be provided to update")
        response = await self._make_request(
            "PUT",
            f"{API_BASE}/collectives/{collective_id}",
            json=json_data,
            headers=self._OCS_HEADERS_JSON,
        )
        data = self._unwrap_ocs(response.json())
        return data["collective"]

    async def trash_collective(self, collective_id: int) -> None:
        """Move a collective to trash (soft delete)."""
        response = await self._make_request(
            "DELETE",
            f"{API_BASE}/collectives/{collective_id}",
            headers=self._OCS_HEADERS,
        )
        self._unwrap_ocs(response.json())

    async def delete_collective(self, collective_id: int) -> None:
        """Permanently delete a collective (must be trashed first).

        This is irreversible. The collective must be in the trash before
        calling this method.
        """
        response = await self._make_request(
            "DELETE",
            f"{API_BASE}/collectives/trash/{collective_id}",
            headers=self._OCS_HEADERS,
        )
        self._unwrap_ocs(response.json())

    # Trash (collectives)

    async def get_trashed_collectives(self) -> list[dict[str, Any]]:
        """List trashed collectives."""
        response = await self._make_request(
            "GET", f"{API_BASE}/collectives/trash", headers=self._OCS_HEADERS
        )
        data = self._unwrap_ocs(response.json())
        return data["collectives"]

    async def restore_collective(self, collective_id: int) -> dict[str, Any]:
        """Restore a collective from trash."""
        response = await self._make_request(
            "PATCH",
            f"{API_BASE}/collectives/trash/{collective_id}",
            headers=self._OCS_HEADERS,
        )
        data = self._unwrap_ocs(response.json())
        return data["collective"]

    # Pages

    async def get_pages(self, collective_id: int) -> list[dict[str, Any]]:
        """List all pages in a collective."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/pages",
            headers=self._OCS_HEADERS,
        )
        data = self._unwrap_ocs(response.json())
        return data["pages"]

    async def get_page(self, collective_id: int, page_id: int) -> dict[str, Any]:
        """Get a single page's metadata."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}",
            headers=self._OCS_HEADERS,
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
            headers=self._OCS_HEADERS_JSON,
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
            headers=self._OCS_HEADERS_JSON,
        )
        data = self._unwrap_ocs(response.json())
        return data["page"]

    async def trash_page(self, collective_id: int, page_id: int) -> None:
        """Move a page to trash (soft delete)."""
        response = await self._make_request(
            "DELETE",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}",
            headers=self._OCS_HEADERS,
        )
        self._unwrap_ocs(response.json())

    async def set_page_emoji(
        self, collective_id: int, page_id: int, emoji: str | None
    ) -> dict[str, Any]:
        """Set or clear the emoji on a page."""
        # Sending {"emoji": null} intentionally clears the emoji on the server
        json_data = {"emoji": emoji}
        response = await self._make_request(
            "PUT",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}/emoji",
            json=json_data,
            headers=self._OCS_HEADERS_JSON,
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
            headers=self._OCS_HEADERS,
        )
        data = self._unwrap_ocs(response.json())
        return data["pages"]

    # Tags

    async def get_tags(self, collective_id: int) -> list[dict[str, Any]]:
        """List all tags in a collective."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/tags",
            headers=self._OCS_HEADERS,
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
            headers=self._OCS_HEADERS_JSON,
        )
        data = self._unwrap_ocs(response.json())
        return data["tag"]

    async def assign_tag(self, collective_id: int, page_id: int, tag_id: int) -> None:
        """Assign a tag to a page."""
        response = await self._make_request(
            "PUT",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}/tags/{tag_id}",
            headers=self._OCS_HEADERS,
        )
        self._unwrap_ocs(response.json())

    async def remove_tag(self, collective_id: int, page_id: int, tag_id: int) -> None:
        """Remove a tag from a page."""
        response = await self._make_request(
            "DELETE",
            f"{API_BASE}/collectives/{collective_id}/pages/{page_id}/tags/{tag_id}",
            headers=self._OCS_HEADERS,
        )
        self._unwrap_ocs(response.json())

    # Trash

    async def get_trashed_pages(self, collective_id: int) -> list[dict[str, Any]]:
        """List trashed pages in a collective."""
        response = await self._make_request(
            "GET",
            f"{API_BASE}/collectives/{collective_id}/pages/trash",
            headers=self._OCS_HEADERS,
        )
        data = self._unwrap_ocs(response.json())
        return data["pages"]

    async def restore_page(self, collective_id: int, page_id: int) -> dict[str, Any]:
        """Restore a page from trash."""
        response = await self._make_request(
            "PATCH",
            f"{API_BASE}/collectives/{collective_id}/pages/trash/{page_id}",
            headers=self._OCS_HEADERS,
        )
        data = self._unwrap_ocs(response.json())
        return data["page"]
