"""Pydantic models for Shopping List app responses."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import BaseResponse, StatusResponse


class ShoppingList(BaseModel):
    """A shopping list."""

    id: int = Field(description="List ID")
    userId: str | None = Field(None, description="UID of the list owner")
    title: str = Field(description="List title")
    permission: int | None = Field(
        None, description="Current user's permission level (0 = read, 1 = write)"
    )
    isOwner: bool | None = Field(None, description="Whether the user owns this list")
    isPinned: bool | None = Field(
        None, description="Whether the user pinned this list in the sidebar"
    )
    createdAt: str | None = Field(None, description="Creation timestamp (ISO 8601)")
    updatedAt: str | None = Field(None, description="Last update timestamp (ISO 8601)")

    # The app gained `isPinned` in 1.5 and may gain more; an unknown field
    # should widen the model's output, not fail the tool.
    model_config = ConfigDict(extra="allow")


class ShoppingListItem(BaseModel):
    """An item on a shopping list."""

    id: int = Field(description="Item ID")
    listId: int = Field(description="ID of the list the item belongs to")
    name: str = Field(description="Item name")
    quantity: str | None = Field(None, description="Free-text quantity, e.g. '2'")
    unit: str | None = Field(None, description="Free-text unit, e.g. 'cups'")
    shopAreaId: int | None = Field(
        None, description="ID of the shop area the item is filed under"
    )
    checked: bool = Field(default=False, description="Whether the item is ticked off")
    checkedBy: str | None = Field(
        None, description="UID of the user who ticked the item off"
    )
    sortOrder: int | None = Field(None, description="Manual sort position")
    tags: list[dict[str, Any]] = Field(
        default_factory=list, description="Tags on the item"
    )
    createdAt: str | None = Field(None, description="Creation timestamp (ISO 8601)")
    updatedAt: str | None = Field(None, description="Last update timestamp (ISO 8601)")

    model_config = ConfigDict(extra="allow")


# Response models for MCP tools


class ListShoppingListsResponse(BaseResponse):
    """Response model for listing shopping lists."""

    lists: list[ShoppingList] = Field(description="The user's shopping lists")
    total_count: int = Field(description="Number of lists returned")


class ShoppingListResponse(BaseResponse):
    """Response model for a single shopping list."""

    list: ShoppingList = Field(description="The shopping list")


class DeleteShoppingListResponse(StatusResponse):
    """Response model for shopping list deletion."""

    deleted_id: int = Field(description="ID of the deleted list")


class ListShoppingListItemsResponse(BaseResponse):
    """Response model for listing the items on a shopping list."""

    items: list[ShoppingListItem] = Field(description="Items on the list")
    list_id: int = Field(description="ID of the list the items belong to")
    total_count: int = Field(description="Number of items returned")


class ShoppingListItemResponse(BaseResponse):
    """Response model for a single shopping list item."""

    item: ShoppingListItem = Field(description="The item")


class AddShoppingListItemsResponse(BaseResponse):
    """Response model for adding several items to a list in one call."""

    items: list[ShoppingListItem] = Field(description="The items that were added")
    list_id: int = Field(description="ID of the list the items were added to")
    added_count: int = Field(description="Number of items added")


class DeleteShoppingListItemResponse(StatusResponse):
    """Response model for item deletion."""

    deleted_id: int = Field(description="ID of the deleted item")
    list_id: int = Field(description="ID of the list the item was on")


class BulkItemActionResponse(StatusResponse):
    """Response model for the list-wide item actions (clear / uncheck all)."""

    list_id: int = Field(description="ID of the list the action applied to")
