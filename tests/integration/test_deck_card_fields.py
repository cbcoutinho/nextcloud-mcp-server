"""Integration tests for the Deck 1.18 card fields ``startdate`` and ``color``
(GH #1530).

Deck's card update nulls ``startdate`` whenever the key is omitted, so these
check that update and cross-board move carry it over, and that both fields
round-trip through the MCP tools. On Deck < 1.18, which ignores the keys, the
tools must refuse them instead.
"""

import json
import uuid
from datetime import timezone

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.capabilities import unmet_capability
from nextcloud_mcp_server.client import NextcloudClient

pytestmark = pytest.mark.integration

# startdate and color both first ship in Deck v1.18.0 (Nextcloud 34).
DECK_CARD_FIELDS_MIN_VERSION = "1.18.0"

START = "2030-05-01T08:00:00Z"


@pytest.fixture
async def card_fields_unsupported(nc_client: NextcloudClient) -> str | None:
    """Why this Deck can't store startdate/color, or None if it can."""
    return await unmet_capability(
        nc_client, nc_client.username, "deck", DECK_CARD_FIELDS_MIN_VERSION
    )


@pytest.fixture
async def card_fields_supported(card_fields_unsupported: str | None) -> None:
    if card_fields_unsupported:
        pytest.skip(card_fields_unsupported)


def _iso(value) -> str | None:
    return (
        value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None
    )


@pytest.mark.usefixtures("card_fields_supported")
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


@pytest.mark.usefixtures("card_fields_supported")
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


async def test_new_fields_are_refused_on_old_deck(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_board_with_card: tuple,
    card_fields_unsupported: str | None,
):
    """Runs on the nc32/nc33 lanes: Deck < 1.18 would silently drop the
    fields, so the tool errors and leaves the card untouched."""
    if not card_fields_unsupported:
        pytest.skip("Deck supports startdate/color here")
    board_data, stack_data, card_data = temporary_board_with_card

    result = await nc_mcp_client.call_tool(
        "deck_update_card",
        {
            "board_id": board_data["id"],
            "stack_id": stack_data["id"],
            "card_id": card_data["id"],
            "title": "Should not be applied",
            "color": "FF0000",
        },
    )

    assert result.is_error is True
    assert "color cannot be set" in result.content[0].text
    card = await nc_client.deck.get_card(
        board_data["id"], stack_data["id"], card_data["id"]
    )
    assert card.title == card_data["title"]
