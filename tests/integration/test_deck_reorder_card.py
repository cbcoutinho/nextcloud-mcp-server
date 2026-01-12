"""Integration tests for Deck card reorder functionality.

Tests issue #469: Moving Deck card from one column (stack) to another not working.
https://github.com/cbcoutinho/nextcloud-mcp-server/issues/469
"""

import logging
import uuid

import pytest

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@pytest.fixture
async def board_with_two_stacks(nc_client: NextcloudClient):
    """Create a temporary board with two stacks for testing card movement.

    Yields:
        tuple: (board_data, source_stack_data, target_stack_data)
    """
    unique_suffix = uuid.uuid4().hex[:8]
    board_title = f"Reorder Test Board {unique_suffix}"
    board = None

    logger.info(f"Creating board with two stacks: {board_title}")
    try:
        board = await nc_client.deck.create_board(board_title, "0000FF")
        board_id = board.id

        # Create source stack (stack 1)
        source_stack = await nc_client.deck.create_stack(
            board_id, f"Source Stack {unique_suffix}", order=1
        )
        source_stack_data = {
            "id": source_stack.id,
            "title": source_stack.title,
            "order": source_stack.order,
        }
        logger.info(f"Created source stack with ID: {source_stack.id}")

        # Create target stack (stack 2)
        target_stack = await nc_client.deck.create_stack(
            board_id, f"Target Stack {unique_suffix}", order=2
        )
        target_stack_data = {
            "id": target_stack.id,
            "title": target_stack.title,
            "order": target_stack.order,
        }
        logger.info(f"Created target stack with ID: {target_stack.id}")

        board_data = {
            "id": board_id,
            "title": board.title,
            "color": board.color,
        }

        yield (board_data, source_stack_data, target_stack_data)

    finally:
        if board:
            logger.info(f"Cleaning up board ID: {board.id}")
            try:
                await nc_client.deck.delete_board(board.id)
            except Exception as e:
                logger.warning(f"Error cleaning up board: {e}")


async def test_reorder_card_move_to_different_stack(
    nc_client: NextcloudClient, board_with_two_stacks: tuple
):
    """Test moving a card from one stack to another (issue #469).

    This test reproduces the bug where the reorder_card API reports success
    but the card doesn't actually move to the target stack.
    """
    board_data, source_stack_data, target_stack_data = board_with_two_stacks
    board_id = board_data["id"]
    source_stack_id = source_stack_data["id"]
    target_stack_id = target_stack_data["id"]

    # Create a card in the source stack
    unique_suffix = uuid.uuid4().hex[:8]
    card_title = f"Test Card {unique_suffix}"
    card = await nc_client.deck.create_card(
        board_id, source_stack_id, card_title, description="Card to be moved"
    )
    card_id = card.id
    logger.info(f"Created card ID: {card_id} in source stack ID: {source_stack_id}")

    try:
        # Verify card is in source stack
        card_before = await nc_client.deck.get_card(board_id, source_stack_id, card_id)
        assert card_before.stackId == source_stack_id, (
            f"Card should start in source stack {source_stack_id}, "
            f"but is in {card_before.stackId}"
        )
        logger.info(f"Verified card is in source stack: {source_stack_id}")

        # Move card to target stack
        logger.info(
            f"Moving card {card_id} from stack {source_stack_id} "
            f"to stack {target_stack_id}"
        )
        await nc_client.deck.reorder_card(
            board_id=board_id,
            stack_id=source_stack_id,
            card_id=card_id,
            order=0,
            target_stack_id=target_stack_id,
        )
        logger.info("reorder_card API call completed")

        # Verify card moved to target stack
        # Note: After moving, the card should be accessible from the target stack
        card_after = await nc_client.deck.get_card(board_id, target_stack_id, card_id)
        assert card_after.stackId == target_stack_id, (
            f"Card should have moved to target stack {target_stack_id}, "
            f"but is in {card_after.stackId}"
        )
        logger.info(f"SUCCESS: Card moved to target stack {target_stack_id}")

    finally:
        # Clean up - try to delete from target stack first, then source
        try:
            await nc_client.deck.delete_card(board_id, target_stack_id, card_id)
        except Exception:
            try:
                await nc_client.deck.delete_card(board_id, source_stack_id, card_id)
            except Exception as e:
                logger.warning(f"Error cleaning up card: {e}")


async def test_reorder_card_within_same_stack(
    nc_client: NextcloudClient, board_with_two_stacks: tuple
):
    """Test reordering a card within the same stack (should work)."""
    board_data, source_stack_data, _ = board_with_two_stacks
    board_id = board_data["id"]
    source_stack_id = source_stack_data["id"]

    # Create two cards in the source stack
    unique_suffix = uuid.uuid4().hex[:8]
    card1 = await nc_client.deck.create_card(
        board_id, source_stack_id, f"Card 1 {unique_suffix}", order=0
    )
    card2 = await nc_client.deck.create_card(
        board_id, source_stack_id, f"Card 2 {unique_suffix}", order=1
    )
    logger.info(f"Created cards {card1.id} (order 0) and {card2.id} (order 1)")

    try:
        # Reorder card1 to position after card2
        await nc_client.deck.reorder_card(
            board_id=board_id,
            stack_id=source_stack_id,
            card_id=card1.id,
            order=2,  # Move to position 2
            target_stack_id=source_stack_id,  # Same stack
        )
        logger.info(f"Reordered card {card1.id} to order 2")

        # Verify card is still in the same stack
        card_after = await nc_client.deck.get_card(board_id, source_stack_id, card1.id)
        assert card_after.stackId == source_stack_id
        logger.info("Card reorder within same stack succeeded")

    finally:
        try:
            await nc_client.deck.delete_card(board_id, source_stack_id, card1.id)
            await nc_client.deck.delete_card(board_id, source_stack_id, card2.id)
        except Exception as e:
            logger.warning(f"Error cleaning up cards: {e}")
