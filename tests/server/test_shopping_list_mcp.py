"""End-to-end coverage of the Shopping List MCP tools against a real stack.

The round-trip mirrors GH #1461's use case: create a list, drop a recipe's
ingredients on it in one call, shop it, tidy up.
"""

import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


def _payload(result):
    assert result.is_error is False, f"MCP tool call failed: {result.content}"
    return json.loads(result.content[0].text)


async def test_mcp_shopping_list_recipe_round_trip(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Create a list, add a recipe's ingredients, tick one off, clear it."""
    title = f"MCP Test List {uuid.uuid4().hex[:8]}"
    list_id = None

    try:
        created = _payload(
            await nc_mcp_client.call_tool(
                "nc_shopping_list_create_list", {"title": title}
            )
        )
        list_id = created["list"]["id"]
        assert created["list"]["title"] == title

        # Verify via the direct client, not only through the tool that made it.
        assert (await nc_client.shopping_list.get_list(list_id))["title"] == title

        # The whole point of #1461: a recipe's ingredients in one call.
        added = _payload(
            await nc_mcp_client.call_tool(
                "nc_shopping_list_add_items",
                {
                    "list_id": list_id,
                    "items": [
                        {"name": "flour", "quantity": "100", "unit": "g"},
                        {"name": "eggs", "quantity": "2"},
                        {"name": "milk", "quantity": "200", "unit": "ml"},
                    ],
                },
            )
        )
        assert added["added_count"] == 3
        assert [item["name"] for item in added["items"]] == ["flour", "eggs", "milk"]
        egg_id = added["items"][1]["id"]

        listed = _payload(
            await nc_mcp_client.call_tool(
                "nc_shopping_list_get_items", {"list_id": list_id}
            )
        )
        assert listed["total_count"] == 3
        assert {item["name"] for item in listed["items"]} == {"flour", "eggs", "milk"}

        updated = _payload(
            await nc_mcp_client.call_tool(
                "nc_shopping_list_update_item",
                {"list_id": list_id, "item_id": egg_id, "quantity": "3"},
            )
        )
        assert updated["item"]["quantity"] == "3"

        checked = _payload(
            await nc_mcp_client.call_tool(
                "nc_shopping_list_check_item",
                {"list_id": list_id, "item_id": egg_id, "checked": True},
            )
        )
        assert checked["item"]["checked"] is True

        _payload(
            await nc_mcp_client.call_tool(
                "nc_shopping_list_clear_checked_items", {"list_id": list_id}
            )
        )
        remaining = await nc_client.shopping_list.get_items(list_id)
        assert {item["name"] for item in remaining} == {"flour", "milk"}

        lists = _payload(
            await nc_mcp_client.call_tool("nc_shopping_list_get_lists", {})
        )
        assert list_id in {entry["id"] for entry in lists["lists"]}

    finally:
        if list_id is not None:
            try:
                await nc_client.shopping_list.delete_list(list_id)
            except Exception as e:
                logger.warning("Failed to clean up shopping list %s: %s", list_id, e)


async def test_mcp_shopping_list_duplicate_name_adds_a_second_row(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Pin the fact the add_items docstring tells the model.

    The app's quantity-merging lives in its Vue store, which matches an existing
    item and issues an update — `ItemService::create` behind `POST /items`
    inserts unconditionally. So the API duplicates, and a tool description
    promising a merge would send a model down the wrong path.
    """
    title = f"MCP Dup Test {uuid.uuid4().hex[:8]}"
    list_id = None

    try:
        created = _payload(
            await nc_mcp_client.call_tool(
                "nc_shopping_list_create_list", {"title": title}
            )
        )
        list_id = created["list"]["id"]

        for _ in range(2):
            _payload(
                await nc_mcp_client.call_tool(
                    "nc_shopping_list_add_items",
                    {"list_id": list_id, "items": [{"name": "flour", "quantity": "1"}]},
                )
            )

        items = await nc_client.shopping_list.get_items(list_id)
        assert [item["name"] for item in items] == ["flour", "flour"]

    finally:
        if list_id is not None:
            try:
                await nc_client.shopping_list.delete_list(list_id)
            except Exception as e:
                logger.warning("Failed to clean up shopping list %s: %s", list_id, e)


async def test_mcp_shopping_list_missing_list_is_a_clean_error(
    nc_mcp_client: ClientSession,
):
    """A 404 from the app must surface as an explained tool error, not a trace."""
    result = await nc_mcp_client.call_tool(
        "nc_shopping_list_get_items", {"list_id": 99999999}
    )

    assert result.is_error is True
    assert "not found" in result.content[0].text.lower()
