"""Client for the Nextcloud Shopping List app (``shopping_list``).

Covers the lists and items surface of the app's OCS API. Shop areas, tags,
shares and the public-link API are deliberately not wrapped yet — nothing asks
for them, and the item payload carries ``shopAreaId`` through untouched, so the
app's own area auto-detection keeps working.

See https://github.com/otherworld-dev/Shopping-List.
"""

import logging
from typing import Any

from .base import BaseNextcloudClient
from .ocs import OCS_REQUEST_HEADERS

logger = logging.getLogger(__name__)


class ShoppingListClient(BaseNextcloudClient):
    """Client for Nextcloud Shopping List app operations."""

    app_name = "shopping_list"

    API_BASE = "/ocs/v2.php/apps/shopping_list/api/v1"

    # Own copy -- see the note in GroupsClient.
    _OCS_HEADERS: dict[str, str] = dict(OCS_REQUEST_HEADERS)

    async def _ocs(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue an OCS request and hand back ``ocs.data``.

        Every route in this app answers with the same envelope, and
        ``/ocs/v2.php`` mirrors the OCS code onto the HTTP status, which
        ``_make_request`` already raises on — so unwrapping is all that is left
        to do. The delete/clear routes answer 204 with an empty body, hence the
        guard.
        """
        response = await self._make_request(
            method, f"{self.API_BASE}{path}", headers=self._OCS_HEADERS, **kwargs
        )
        if not response.content:
            return None
        return response.json()["ocs"]["data"]

    # --- Lists ---

    async def get_lists(self) -> list[dict[str, Any]]:
        """Get every list the user owns or has had shared with them."""
        return await self._ocs("GET", "/lists")

    async def get_list(self, list_id: int) -> dict[str, Any]:
        """Get a single list.

        Raises:
            HTTPStatusError: 404 if the list does not exist or is not shared
                with the user.
        """
        return await self._ocs("GET", f"/lists/{list_id}")

    async def create_list(self, title: str) -> dict[str, Any]:
        """Create a new list owned by the user."""
        return await self._ocs("POST", "/lists", json={"title": title})

    async def update_list(self, list_id: int, title: str) -> dict[str, Any]:
        """Rename a list.

        Raises:
            HTTPStatusError: 404 if not found, 403 without write access.
        """
        return await self._ocs("PUT", f"/lists/{list_id}", json={"title": title})

    async def delete_list(self, list_id: int) -> None:
        """Delete a list and everything on it.

        Raises:
            HTTPStatusError: 404 if not found, 403 if the user is not the owner.
        """
        await self._ocs("DELETE", f"/lists/{list_id}")

    # --- Items ---

    async def get_items(self, list_id: int) -> list[dict[str, Any]]:
        """Get every item on a list, checked ones included."""
        return await self._ocs("GET", f"/lists/{list_id}/items")

    async def add_item(
        self,
        list_id: int,
        name: str,
        quantity: str | None = None,
        unit: str | None = None,
        shop_area_id: int | None = None,
        checked: bool = False,
    ) -> dict[str, Any]:
        """Add an item to a list.

        Args:
            list_id: List to add to.
            name: Item name, e.g. "flour".
            quantity: Free-text amount, e.g. "2". Defaults to "1" server-side.
            unit: Free-text unit, e.g. "cups".
            shop_area_id: Shop area to file the item under. Left unset, the app
                assigns one from its keyword mappings.
            checked: Add the item already ticked off.

        Raises:
            HTTPStatusError: 404 if the list is missing, 403 without write
                access.
        """
        payload: dict[str, Any] = {"name": name, "checked": checked}
        if quantity is not None:
            payload["quantity"] = quantity
        if unit is not None:
            payload["unit"] = unit
        if shop_area_id is not None:
            payload["shopAreaId"] = shop_area_id
        return await self._ocs("POST", f"/lists/{list_id}/items", json=payload)

    async def update_item(
        self, list_id: int, item_id: int, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Update an item's fields.

        Args:
            fields: Only the keys present are changed — the app reads the
                request params rather than a full entity, so an omitted field
                keeps its value while an explicit ``None`` clears it.
        """
        return await self._ocs("PUT", f"/lists/{list_id}/items/{item_id}", json=fields)

    async def check_item(
        self, list_id: int, item_id: int, checked: bool
    ) -> dict[str, Any]:
        """Tick an item off, or put it back on the list."""
        return await self._ocs(
            "PUT", f"/lists/{list_id}/items/{item_id}/check", json={"checked": checked}
        )

    async def delete_item(self, list_id: int, item_id: int) -> None:
        """Delete a single item."""
        await self._ocs("DELETE", f"/lists/{list_id}/items/{item_id}")

    async def clear_checked_items(self, list_id: int) -> None:
        """Delete every ticked-off item on a list."""
        await self._ocs("DELETE", f"/lists/{list_id}/items/checked")

    async def uncheck_all_items(self, list_id: int) -> None:
        """Put every ticked-off item on a list back into the open section."""
        await self._ocs("POST", f"/lists/{list_id}/items/uncheck-all")
