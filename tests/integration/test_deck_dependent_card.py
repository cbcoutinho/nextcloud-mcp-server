"""Integration tests for Deck card dependencies ("Add dependent card").

Exercises the assign/remove dependent-card endpoints against a live Deck
instance and verifies the ``dependentCards`` relation on the depending card.
"""

import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.capabilities import unmet_capability
from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration

# Card dependencies first ship in Deck v1.18.0 (Nextcloud 34+). On older
# instances the API endpoints don't exist and the MCP tools are capability-gated
# out, so these tests skip rather than fail.
DECK_DEPENDENCIES_MIN_VERSION = "1.18.0"


async def _skip_without_dependency_support(nc_client: NextcloudClient) -> None:
    reason = await unmet_capability(
        nc_client, nc_client.username, "deck", DECK_DEPENDENCIES_MIN_VERSION
    )
    if reason:
        pytest.skip(reason)


@pytest.fixture
async def board_with_two_cards(nc_client: NextcloudClient):
    """Create a temporary board with one stack and two cards.

    Yields:
        tuple: (board_id, stack_id, card_id, dependency_card_id)
    """
    await _skip_without_dependency_support(nc_client)
    unique_suffix = uuid.uuid4().hex[:8]
    board = None
    try:
        board = await nc_client.deck.create_board(
            f"Dependency Test Board {unique_suffix}", "0000FF"
        )
        stack = await nc_client.deck.create_stack(
            board.id, f"Stack {unique_suffix}", order=1
        )
        card = await nc_client.deck.create_card(
            board.id, stack.id, f"Depending Card {unique_suffix}"
        )
        dependency = await nc_client.deck.create_card(
            board.id, stack.id, f"Dependency Card {unique_suffix}"
        )
        yield (board.id, stack.id, card.id, dependency.id)
    finally:
        if board:
            try:
                await nc_client.deck.delete_board(board.id)
            except Exception:
                logger.warning("Error cleaning up board %s", board.id, exc_info=True)


async def test_assign_and_remove_dependent_card(
    nc_client: NextcloudClient, board_with_two_cards: tuple
):
    """A card records the dependency, then drops it, on ``dependentCards``."""
    board_id, stack_id, card_id, dependency_card_id = board_with_two_cards

    # Starts with no dependencies
    before = await nc_client.deck.get_card(board_id, stack_id, card_id)
    assert not before.dependentCards

    # Assign: both the returned card and a fresh fetch show the dependency
    assigned = await nc_client.deck.assign_dependent_card(
        board_id, stack_id, card_id, dependency_card_id
    )
    assert assigned.dependentCards == [dependency_card_id]

    after_assign = await nc_client.deck.get_card(board_id, stack_id, card_id)
    assert after_assign.dependentCards == [dependency_card_id]

    # Remove: dependency is gone again
    removed = await nc_client.deck.remove_dependent_card(
        board_id, stack_id, card_id, dependency_card_id
    )
    assert not removed.dependentCards

    after_remove = await nc_client.deck.get_card(board_id, stack_id, card_id)
    assert not after_remove.dependentCards


async def test_assign_and_remove_dependent_card_via_mcp(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_board_with_card: tuple,
):
    """The same round-trip driven through the MCP tools, with the dependency
    read back via the ``deck_get_card`` tool."""
    await _skip_without_dependency_support(nc_client)
    board_data, stack_data, card_data = temporary_board_with_card
    board_id = board_data["id"]
    stack_id = stack_data["id"]
    card_id = card_data["id"]

    # A second card on the same stack to depend on.
    dependency = await nc_client.deck.create_card(
        board_id, stack_id, f"Dependency Card {uuid.uuid4().hex[:8]}"
    )
    args = {
        "board_id": board_id,
        "stack_id": stack_id,
        "card_id": card_id,
        "dependent_card_id": dependency.id,
    }

    async def get_dependent_cards() -> list:
        result = await nc_mcp_client.call_tool(
            "deck_get_card",
            {"board_id": board_id, "stack_id": stack_id, "card_id": card_id},
        )
        assert result.is_error is False, f"get_card failed: {result.content}"
        return json.loads(result.content[0].text).get("dependentCards") or []

    # Assign via MCP
    assign_result = await nc_mcp_client.call_tool("deck_assign_dependent_card", args)
    assert assign_result.is_error is False, f"assign failed: {assign_result.content}"
    assert json.loads(assign_result.content[0].text)["success"] is True
    assert await get_dependent_cards() == [dependency.id]

    # Remove via MCP
    remove_result = await nc_mcp_client.call_tool("deck_remove_dependent_card", args)
    assert remove_result.is_error is False, f"remove failed: {remove_result.content}"
    assert json.loads(remove_result.content[0].text)["success"] is True
    assert await get_dependent_cards() == []
