"""Integration tests for the Deck 1.18 card fields ``startdate`` and ``color``
(GH #1530).

Deck's card update nulls ``startdate`` whenever the key is omitted, so these
check that update and cross-board move carry it over, and that both fields
round-trip through the MCP tools.
"""

import json
import uuid
from datetime import timezone

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.capabilities import unmet_capability
from nextcloud_mcp_server.client import NextcloudClient

pytestmark = pytest.mark.integration

# startdate and color both first ship in Deck v1.18.0. Older Deck ignores the
# keys, so there is nothing to assert there.
DECK_CARD_FIELDS_MIN_VERSION = "1.18.0"

START = "2030-05-01T08:00:00Z"


@pytest.fixture(autouse=True)
async def _skip_without_card_fields(nc_client: NextcloudClient):
    reason = await unmet_capability(
        nc_client, nc_client.username, "deck", DECK_CARD_FIELDS_MIN_VERSION
    )
    if reason:
        pytest.skip(reason)


def _iso(value) -> str | None:
    return (
        value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None
    )


async def test_startdate_and_color_round_trip_via_mcp(
    nc_mcp_client: ClientSession, temporary_board_with_stack: tuple
):
    board_data, stack_data = temporary_board_with_stack
    board_id, stack_id = board_data["id"], stack_data["id"]

    created = await nc_mcp_client.call_tool(
        "deck_create_card",
        {
            "board_id": board_id,
            "stack_id": stack_id,
            "title": f"Fields {uuid.uuid4().hex[:8]}",
            "startdate": START,
            "color": "#FF0000",
        },
    )
    assert created.is_error is False, created.content
    card_id = json.loads(created.content[0].text)["id"]
    ref = {"board_id": board_id, "stack_id": stack_id, "card_id": card_id}

    async def get_card() -> dict:
        result = await nc_mcp_client.call_tool("deck_get_card", ref)
        assert result.is_error is False, result.content
        return json.loads(result.content[0].text)

    card = await get_card()
    assert card["startdate"].startswith("2030-05-01T08:00:00")
    assert card["color"] == "FF0000"

    # A title-only update must keep both.
    result = await nc_mcp_client.call_tool(
        "deck_update_card", {**ref, "title": "Renamed"}
    )
    assert result.is_error is False, result.content
    card = await get_card()
    assert card["startdate"].startswith("2030-05-01T08:00:00")
    assert card["color"] == "FF0000"

    # Empty strings clear both.
    result = await nc_mcp_client.call_tool(
        "deck_update_card", {**ref, "startdate": "", "color": ""}
    )
    assert result.is_error is False, result.content
    card = await get_card()
    assert card["startdate"] is None
    assert card["color"] is None


async def test_move_card_to_board_preserves_startdate_and_color(
    nc_client: NextcloudClient, temporary_board_with_stack: tuple
):
    board_data, stack_data = temporary_board_with_stack
    card = await nc_client.deck.create_card(
        board_data["id"],
        stack_data["id"],
        f"Move {uuid.uuid4().hex[:8]}",
        startdate=START,
        color="00FF00",
    )
    target = await nc_client.deck.create_board(
        f"Move target {uuid.uuid4().hex[:8]}", "0000FF"
    )
    try:
        target_stack = await nc_client.deck.create_stack(target.id, "Target", 1)
        await nc_client.deck.move_card_to_board(
            board_data["id"], stack_data["id"], card.id, target.id, target_stack.id
        )
        moved = await nc_client.deck.get_card(target.id, target_stack.id, card.id)
        assert _iso(moved.startdate) == START
        assert moved.color == "00FF00"
    finally:
        await nc_client.deck.delete_board(target.id)
