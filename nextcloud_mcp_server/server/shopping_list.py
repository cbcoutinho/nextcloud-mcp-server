"""MCP tools for the Nextcloud Shopping List app.

The app answers every failure with the same three statuses — 404 for a missing
list or item, 403 for one the user may read but not write, and anything else is
a server fault — so the translation to ``MCPError`` lives in one context
manager rather than a try/except per tool.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from httpx import HTTPStatusError, RequestError
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.shopping_list import (
    AddShoppingListItemsResponse,
    BulkItemActionResponse,
    DeleteShoppingListItemResponse,
    DeleteShoppingListResponse,
    ListShoppingListItemsResponse,
    ListShoppingListsResponse,
    ShoppingList,
    ShoppingListItem,
    ShoppingListItemInput,
    ShoppingListItemResponse,
    ShoppingListResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


@contextmanager
def _shopping_list_errors(action: str) -> Iterator[None]:
    """Re-raise a Shopping List failure as the message the model should act on."""
    try:
        yield
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error {action}: {e}")
    except HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            detail = "not found, or not shared with you"
        elif status == 403:
            detail = "you do not have write access to this list"
        else:
            detail = f"server error ({status})"
        raise MCPError(code=-1, message=f"Failed {action}: {detail}")


def configure_shopping_list_tools(mcp: MCPServer):
    """Configure Shopping List app MCP tools."""

    @mcp.tool(
        title="List Shopping Lists",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("shopping_list.read")
    @instrument_tool
    async def nc_shopping_list_get_lists(ctx: Context) -> ListShoppingListsResponse:
        """List every shopping list the user owns or has had shared with them."""
        client = await get_client(ctx)
        with _shopping_list_errors("listing shopping lists"):
            lists = [
                ShoppingList(**item) for item in await client.shopping_list.get_lists()
            ]
        return ListShoppingListsResponse(lists=lists, total_count=len(lists))

    @mcp.tool(
        title="Get Shopping List",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("shopping_list.read")
    @instrument_tool
    async def nc_shopping_list_get_list(
        list_id: int, ctx: Context
    ) -> ShoppingListResponse:
        """Get a single shopping list by ID."""
        client = await get_client(ctx)
        with _shopping_list_errors(f"getting shopping list {list_id}"):
            data = await client.shopping_list.get_list(list_id)
        return ShoppingListResponse(list=ShoppingList(**data))

    @mcp.tool(
        title="Create Shopping List",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_create_list(
        title: str, ctx: Context
    ) -> ShoppingListResponse:
        """Create a new shopping list.

        Titles are not unique — calling this twice with the same title makes two
        lists.
        """
        client = await get_client(ctx)
        with _shopping_list_errors(f"creating shopping list '{title}'"):
            data = await client.shopping_list.create_list(title)
        return ShoppingListResponse(list=ShoppingList(**data))

    @mcp.tool(
        title="Rename Shopping List",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_update_list(
        list_id: int, title: str, ctx: Context
    ) -> ShoppingListResponse:
        """Rename an existing shopping list."""
        client = await get_client(ctx)
        with _shopping_list_errors(f"renaming shopping list {list_id}"):
            data = await client.shopping_list.update_list(list_id, title)
        return ShoppingListResponse(list=ShoppingList(**data))

    @mcp.tool(
        title="Delete Shopping List",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_delete_list(
        list_id: int, ctx: Context
    ) -> DeleteShoppingListResponse:
        """Delete a shopping list and every item on it. Only the owner may do this."""
        client = await get_client(ctx)
        with _shopping_list_errors(f"deleting shopping list {list_id}"):
            await client.shopping_list.delete_list(list_id)
        return DeleteShoppingListResponse(
            deleted_id=list_id, message=f"Deleted shopping list {list_id}"
        )

    @mcp.tool(
        title="List Shopping List Items",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("shopping_list.read")
    @instrument_tool
    async def nc_shopping_list_get_items(
        list_id: int, ctx: Context
    ) -> ListShoppingListItemsResponse:
        """Get every item on a shopping list, ticked-off ones included."""
        client = await get_client(ctx)
        with _shopping_list_errors(f"listing items on shopping list {list_id}"):
            items = [
                ShoppingListItem(**item)
                for item in await client.shopping_list.get_items(list_id)
            ]
        return ListShoppingListItemsResponse(
            items=items, list_id=list_id, total_count=len(items)
        )

    @mcp.tool(
        title="Add Shopping List Items",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_add_items(
        list_id: int,
        items: list[ShoppingListItemInput],
        ctx: Context,
    ) -> AddShoppingListItemsResponse:
        """Add one or more items to a shopping list.

        Takes a list so a whole recipe's ingredients land in one call, e.g.
        ``[{"name": "flour", "quantity": "2", "unit": "cups"},
        {"name": "eggs", "quantity": "3"}]``. See the ``items`` schema for the
        fields each entry accepts — only ``name`` is required.

        The whole list is validated before anything is sent, so a malformed
        entry adds nothing. Once sending starts the items go one at a time — the
        app has no bulk endpoint — so a failure partway through leaves the
        earlier items on the list, and the error says how many those were.

        A name already on the list is added again as a **second row**. The
        quantity-merging the Shopping List web UI does is client-side, so the
        API does not do it for you. To top up an existing item instead, read the
        list with ``nc_shopping_list_get_items`` and use
        ``nc_shopping_list_update_item``.
        """
        if not items:
            raise MCPError(code=-1, message="No items given to add")

        client = await get_client(ctx)
        added: list[ShoppingListItem] = []
        for item in items:
            with _shopping_list_errors(
                f"adding '{item.name}' to shopping list {list_id} "
                f"({len(added)} of {len(items)} item(s) already added)"
            ):
                data = await client.shopping_list.add_item(
                    list_id,
                    name=item.name,
                    quantity=item.quantity,
                    unit=item.unit,
                    shop_area_id=item.shop_area_id,
                    checked=item.checked,
                )
            added.append(ShoppingListItem(**data))

        return AddShoppingListItemsResponse(
            items=added, list_id=list_id, added_count=len(added)
        )

    @mcp.tool(
        title="Update Shopping List Item",
        # Idempotent, unlike most update tools here: the app has no etag or
        # version on an item, so ADR-017's "HTTP PUT without version control"
        # case applies — the same fields twice leave the same end state.
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_update_item(
        list_id: int,
        item_id: int,
        ctx: Context,
        name: str | None = None,
        quantity: str | None = None,
        unit: str | None = None,
        shop_area_id: int | None = None,
    ) -> ShoppingListItemResponse:
        """Update an item's name, quantity, unit or shop area.

        Only the arguments given are changed. Omitted ones keep their value,
        which also means this tool can set a field but not *clear* one back to
        empty — pass a new value, or delete and re-add the item. Use
        ``nc_shopping_list_check_item`` to tick an item off — this tool does not
        touch the checked state.
        """
        # Only the keys present are sent: the app reads request params, so an
        # absent key leaves that field alone.
        given: dict[str, Any] = {
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "shopAreaId": shop_area_id,
        }
        fields = {key: value for key, value in given.items() if value is not None}
        if not fields:
            raise MCPError(code=-1, message="No fields given to update")

        client = await get_client(ctx)
        with _shopping_list_errors(f"updating item {item_id} on list {list_id}"):
            data = await client.shopping_list.update_item(list_id, item_id, fields)
        return ShoppingListItemResponse(item=ShoppingListItem(**data))

    @mcp.tool(
        title="Check Shopping List Item",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_check_item(
        list_id: int, item_id: int, checked: bool, ctx: Context
    ) -> ShoppingListItemResponse:
        """Tick an item off the list, or put it back (``checked=false``)."""
        client = await get_client(ctx)
        with _shopping_list_errors(f"checking item {item_id} on list {list_id}"):
            data = await client.shopping_list.check_item(list_id, item_id, checked)
        return ShoppingListItemResponse(item=ShoppingListItem(**data))

    @mcp.tool(
        title="Delete Shopping List Item",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_delete_item(
        list_id: int, item_id: int, ctx: Context
    ) -> DeleteShoppingListItemResponse:
        """Delete a single item from a shopping list."""
        client = await get_client(ctx)
        with _shopping_list_errors(f"deleting item {item_id} on list {list_id}"):
            await client.shopping_list.delete_item(list_id, item_id)
        return DeleteShoppingListItemResponse(
            deleted_id=item_id,
            list_id=list_id,
            message=f"Deleted item {item_id} from shopping list {list_id}",
        )

    @mcp.tool(
        title="Clear Checked Shopping List Items",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_clear_checked_items(
        list_id: int, ctx: Context
    ) -> BulkItemActionResponse:
        """Delete every ticked-off item on a shopping list."""
        client = await get_client(ctx)
        with _shopping_list_errors(f"clearing checked items on list {list_id}"):
            await client.shopping_list.clear_checked_items(list_id)
        return BulkItemActionResponse(
            list_id=list_id,
            message=f"Cleared checked items from shopping list {list_id}",
        )

    @mcp.tool(
        title="Uncheck All Shopping List Items",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("shopping_list.write")
    @instrument_tool
    async def nc_shopping_list_uncheck_all_items(
        list_id: int, ctx: Context
    ) -> BulkItemActionResponse:
        """Put every ticked-off item on a shopping list back among the open items."""
        client = await get_client(ctx)
        with _shopping_list_errors(f"unchecking all items on list {list_id}"):
            await client.shopping_list.uncheck_all_items(list_id)
        return BulkItemActionResponse(
            list_id=list_id,
            message=f"Unchecked all items on shopping list {list_id}",
        )
