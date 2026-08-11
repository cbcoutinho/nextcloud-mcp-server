"""Integration tests for Deck card dependencies ("Add dependent card").

Exercises the assign/remove dependent-card endpoints against a live Deck
instance and verifies the ``dependentCards`` relation on the depending card.
"""

import logging
import uuid

import pytest

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@pytest.fixture
async def board_with_two_cards(nc_client: NextcloudClient):
    """Create a temporary board with one stack and two cards.

    Yields:
        tuple: (board_id, stack_id, card_id, dependency_card_id)
    """
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
            except Exception as e:
                logger.warning("Error cleaning up board: %s", e)


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
