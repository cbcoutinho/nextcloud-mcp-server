import logging
import uuid

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.models.deck import DeckCard, DeckLabel, DeckStack

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


# Board CRUD Tests


async def test_deck_board_crud_workflow(
    nc_client: NextcloudClient, temporary_board: dict
):
    """
    Test complete board CRUD workflow using the temporary_board fixture.
    """
    board_data = temporary_board
    board_id = board_data["id"]
    original_title = board_data["title"]
    original_color = board_data["color"]

    logger.info(f"Testing CRUD operations on board ID: {board_id}")

    # Read the board
    read_board = await nc_client.deck.get_board(board_id)
    assert read_board.id == board_id
    assert read_board.title == original_title
    assert read_board.color == original_color
    logger.info(f"Successfully read board ID: {board_id}")

    # Update the board
    updated_title = f"Updated {original_title}"
    updated_color = "00FF00"  # Green color
    await nc_client.deck.update_board(
        board_id, title=updated_title, color=updated_color
    )

    # Verify the update
    updated_board = await nc_client.deck.get_board(board_id)
    assert updated_board.title == updated_title
    assert updated_board.color == updated_color
    logger.info(f"Successfully updated board ID: {board_id}")


async def test_deck_list_boards(nc_client: NextcloudClient):
    """
    Test listing all boards with different options.
    """
    # Test basic listing
    boards = await nc_client.deck.get_boards()
    assert isinstance(boards, list)
    logger.info(f"Found {len(boards)} boards")

    # Test with details
    detailed_boards = await nc_client.deck.get_boards(details=True)
    assert isinstance(detailed_boards, list)
    logger.info(f"Found {len(detailed_boards)} boards with details")


async def test_deck_board_operations_nonexistent(nc_client: NextcloudClient):
    """
    Test operations on non-existent board return appropriate errors.
    """
    non_existent_id = 999999999

    # Test get non-existent board
    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.deck.get_board(non_existent_id)
    assert excinfo.value.response.status_code in [
        404,
        403,
    ]  # 403 might be returned for access denied
    logger.info(
        f"Get non-existent board correctly failed with {excinfo.value.response.status_code}"
    )

    # Test update non-existent board
    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.deck.update_board(non_existent_id, title="Should Fail")
    assert excinfo.value.response.status_code in [
        404,
        403,
        400,
    ]  # 400 for bad request on invalid board ID
    logger.info(
        f"Update non-existent board correctly failed with {excinfo.value.response.status_code}"
    )


# Stack CRUD Tests


async def test_deck_stack_crud_workflow(
    nc_client: NextcloudClient, temporary_board: dict
):
    """
    Test complete stack CRUD workflow.
    """
    board_id = temporary_board["id"]
    stack_title = f"Test Stack {uuid.uuid4().hex[:8]}"
    stack_order = 1
    stack = None

    try:
        # Create stack
        stack = await nc_client.deck.create_stack(board_id, stack_title, stack_order)
        assert isinstance(stack, DeckStack)
        assert stack.title == stack_title
        assert stack.order == stack_order
        stack_id = stack.id
        logger.info(f"Created stack ID: {stack_id}")

        # Read stack
        read_stack = await nc_client.deck.get_stack(board_id, stack_id)
        assert read_stack.id == stack_id
        assert read_stack.title == stack_title
        logger.info(f"Successfully read stack ID: {stack_id}")

        # Update stack
        updated_title = f"Updated {stack_title}"
        updated_order = 2
        await nc_client.deck.update_stack(
            board_id, stack_id, title=updated_title, order=updated_order
        )

        # Verify update
        updated_stack = await nc_client.deck.get_stack(board_id, stack_id)
        assert updated_stack.title == updated_title
        assert updated_stack.order == updated_order
        logger.info(f"Successfully updated stack ID: {stack_id}")

        # List stacks
        stacks = await nc_client.deck.get_stacks(board_id)
        assert isinstance(stacks, list)
        assert any(s.id == stack_id for s in stacks)
        logger.info(f"Found stack ID: {stack_id} in board stacks list")

    finally:
        # Clean up - delete stack
        if stack and hasattr(stack, "id"):
            try:
                await nc_client.deck.delete_stack(board_id, stack.id)
                logger.info(f"Cleaned up stack ID: {stack.id}")
            except Exception as e:
                logger.warning(f"Failed to clean up stack ID: {stack.id}: {e}")


# Card CRUD Tests


async def test_deck_card_crud_workflow(
    nc_client: NextcloudClient, temporary_board_with_stack: tuple
):
    """
    Test complete card CRUD workflow.
    """
    board_data, stack_data = temporary_board_with_stack
    board_id = board_data["id"]
    stack_id = stack_data["id"]

    card_title = f"Test Card {uuid.uuid4().hex[:8]}"
    card_description = f"Test description for card {uuid.uuid4().hex[:8]}"
    card = None

    try:
        # Create card
        card = await nc_client.deck.create_card(
            board_id, stack_id, card_title, description=card_description
        )
        assert isinstance(card, DeckCard)
        assert card.title == card_title
        assert card.description == card_description
        card_id = card.id
        logger.info(f"Created card ID: {card_id}")

        # Read card
        read_card = await nc_client.deck.get_card(board_id, stack_id, card_id)
        assert read_card.id == card_id
        assert read_card.title == card_title
        logger.info(f"Successfully read card ID: {card_id}")

        # Update card
        updated_title = f"Updated {card_title}"
        updated_description = f"Updated description for {card_title}"
        await nc_client.deck.update_card(
            board_id,
            stack_id,
            card_id,
            title=updated_title,
            description=updated_description,
        )

        # Verify update
        updated_card = await nc_client.deck.get_card(board_id, stack_id, card_id)
        assert updated_card.title == updated_title
        assert updated_card.description == updated_description
        logger.info(f"Successfully updated card ID: {card_id}")

        # Archive and unarchive card
        await nc_client.deck.archive_card(board_id, stack_id, card_id)
        logger.info(f"Archived card ID: {card_id}")

        await nc_client.deck.unarchive_card(board_id, stack_id, card_id)
        logger.info(f"Unarchived card ID: {card_id}")

    finally:
        # Clean up - delete card
        if card and hasattr(card, "id"):
            try:
                await nc_client.deck.delete_card(board_id, stack_id, card.id)
                logger.info(f"Cleaned up card ID: {card.id}")
            except Exception as e:
                logger.warning(f"Failed to clean up card ID: {card.id}: {e}")


# Label CRUD Tests


async def test_deck_label_crud_workflow(
    nc_client: NextcloudClient, temporary_board: dict
):
    """
    Test complete label CRUD workflow.
    """
    board_id = temporary_board["id"]
    label_title = f"Test Label {uuid.uuid4().hex[:8]}"
    label_color = "FF0000"  # Red
    label = None

    try:
        # Create label
        label = await nc_client.deck.create_label(board_id, label_title, label_color)
        assert isinstance(label, DeckLabel)
        assert label.title == label_title
        assert label.color == label_color
        label_id = label.id
        logger.info(f"Created label ID: {label_id}")

        # Read label
        read_label = await nc_client.deck.get_label(board_id, label_id)
        assert read_label.id == label_id
        assert read_label.title == label_title
        logger.info(f"Successfully read label ID: {label_id}")

        # Update label
        updated_title = f"Updated {label_title}"
        updated_color = "00FF00"  # Green
        await nc_client.deck.update_label(
            board_id, label_id, title=updated_title, color=updated_color
        )

        # Verify update
        updated_label = await nc_client.deck.get_label(board_id, label_id)
        assert updated_label.title == updated_title
        assert updated_label.color == updated_color
        logger.info(f"Successfully updated label ID: {label_id}")

    finally:
        # Clean up - delete label
        if label and hasattr(label, "id"):
            try:
                await nc_client.deck.delete_label(board_id, label.id)
                logger.info(f"Cleaned up label ID: {label.id}")
            except Exception as e:
                logger.warning(f"Failed to clean up label ID: {label.id}: {e}")


# Configuration and Comments Tests


async def test_deck_config_operations(nc_client: NextcloudClient):
    """
    Test deck configuration operations.
    """
    # Get config
    config = await nc_client.deck.get_config()
    assert config is not None
    logger.info(f"Retrieved deck config: {config}")


async def test_deck_comments_workflow(
    nc_client: NextcloudClient, temporary_board_with_card: tuple
):
    """
    Test comment operations on a card.
    """
    board_data, stack_data, card_data = temporary_board_with_card
    card_id = card_data["id"]

    comment_message = f"Test comment {uuid.uuid4().hex[:8]}"
    comment = None

    try:
        # Create comment
        comment = await nc_client.deck.create_comment(card_id, comment_message)
        assert comment.message == comment_message
        comment_id = comment.id
        logger.info(f"Created comment ID: {comment_id}")

        # List comments
        comments = await nc_client.deck.get_comments(card_id)
        assert isinstance(comments, list)
        assert any(c.id == comment_id for c in comments)
        logger.info(f"Found comment ID: {comment_id} in card comments")

        # Update comment
        updated_message = f"Updated {comment_message}"
        updated_comment = await nc_client.deck.update_comment(
            card_id, comment_id, updated_message
        )
        assert updated_comment.message == updated_message
        logger.info(f"Successfully updated comment ID: {comment_id}")

    finally:
        # Clean up - delete comment
        if comment and hasattr(comment, "id"):
            try:
                await nc_client.deck.delete_comment(card_id, comment.id)
                logger.info(f"Cleaned up comment ID: {comment.id}")
            except Exception as e:
                logger.warning(f"Failed to clean up comment ID: {comment.id}: {e}")
