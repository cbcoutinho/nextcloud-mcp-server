"""Integration tests for moving Deck cards between boards.

Covers ``move_card_to_board`` and the same-board restriction on
``reorder_card``. The key behaviour under test is that a cross-board move
remaps the card's board-scoped labels to the destination board (by title)
rather than leaving orphaned labels that still reference the source board.
"""

import logging
import uuid

import pytest

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@pytest.fixture
async def two_boards_with_stacks(nc_client: NextcloudClient):
    """Create two temporary boards, each with a single stack.

    Yields:
        tuple: (source_board_id, source_stack_id, target_board_id, target_stack_id)
    """
    unique_suffix = uuid.uuid4().hex[:8]
    source_board = None
    target_board = None
    try:
        source_board = await nc_client.deck.create_board(
            f"Move Test Source {unique_suffix}", "FF0000"
        )
        target_board = await nc_client.deck.create_board(
            f"Move Test Target {unique_suffix}", "0000FF"
        )
        source_stack = await nc_client.deck.create_stack(
            source_board.id, f"Source Stack {unique_suffix}", order=1
        )
        target_stack = await nc_client.deck.create_stack(
            target_board.id, f"Target Stack {unique_suffix}", order=1
        )
        logger.info(
            "Created source board %s/stack %s and target board %s/stack %s",
            source_board.id,
            source_stack.id,
            target_board.id,
            target_stack.id,
        )
        yield (source_board.id, source_stack.id, target_board.id, target_stack.id)
    finally:
        for board in (source_board, target_board):
            if board:
                try:
                    await nc_client.deck.delete_board(board.id)
                except Exception as e:
                    logger.warning("Error cleaning up board %s: %s", board.id, e)


async def test_move_card_to_board_preserves_identity(
    nc_client: NextcloudClient, two_boards_with_stacks: tuple
):
    """A cross-board move relocates the card (same id) to the target board."""
    source_board_id, source_stack_id, target_board_id, target_stack_id = (
        two_boards_with_stacks
    )

    suffix = uuid.uuid4().hex[:8]
    card = await nc_client.deck.create_card(
        source_board_id, source_stack_id, f"Move me {suffix}", description="payload"
    )
    logger.info("Created card %s on source board %s", card.id, source_board_id)

    moved = await nc_client.deck.move_card_to_board(
        source_board_id=source_board_id,
        source_stack_id=source_stack_id,
        card_id=card.id,
        target_board_id=target_board_id,
        target_stack_id=target_stack_id,
    )

    # Same card id, now on the target stack, with its description preserved
    assert moved.id == card.id
    assert moved.stackId == target_stack_id

    # The card is readable on the target board and gone from the source stack
    on_target = await nc_client.deck.get_card(target_board_id, target_stack_id, card.id)
    assert on_target.stackId == target_stack_id
    assert on_target.description == "payload"

    source_cards = await nc_client.deck.get_stack(source_board_id, source_stack_id)
    source_card_ids = {c.id for c in (source_cards.cards or [])}
    assert card.id not in source_card_ids


async def test_move_card_to_board_remaps_labels(
    nc_client: NextcloudClient, two_boards_with_stacks: tuple
):
    """A board-scoped label is remapped to the destination board, not orphaned.

    Deck auto-creates the same default labels (e.g. "Finished") on every board,
    so the moved card's "Finished" label should end up pointing at the target
    board's "Finished" label rather than keeping the source board's id.
    """
    source_board_id, source_stack_id, target_board_id, target_stack_id = (
        two_boards_with_stacks
    )

    # "Finished" is one of the four labels Deck auto-creates on every new
    # board, so it is reliably present on both the source and target boards.
    source_board = await nc_client.deck.get_board(source_board_id)
    target_board = await nc_client.deck.get_board(target_board_id)
    source_label = next(
        label for label in source_board.labels if label.title == "Finished"
    )
    target_label = next(
        label for label in target_board.labels if label.title == "Finished"
    )

    suffix = uuid.uuid4().hex[:8]
    card = await nc_client.deck.create_card(
        source_board_id, source_stack_id, f"Labeled {suffix}"
    )
    await nc_client.deck.assign_label_to_card(
        source_board_id, source_stack_id, card.id, source_label.id
    )

    # Sanity check: the card carries the source board's label before the move
    before = await nc_client.deck.get_card(source_board_id, source_stack_id, card.id)
    assert any(label.id == source_label.id for label in (before.labels or []))

    await nc_client.deck.move_card_to_board(
        source_board_id=source_board_id,
        source_stack_id=source_stack_id,
        card_id=card.id,
        target_board_id=target_board_id,
        target_stack_id=target_stack_id,
    )

    after = await nc_client.deck.get_card(target_board_id, target_stack_id, card.id)
    labels = after.labels or []
    assert labels, "Card lost its label during the move"

    finished = [label for label in labels if label.title == "Finished"]
    assert finished, "The 'Finished' label was not preserved across the move"
    # Remapped to the target board's label; the source board's label is gone
    assert all(label.boardId == target_board_id for label in finished), (
        f"Label still references source board: {[label.boardId for label in finished]}"
    )
    assert any(label.id == target_label.id for label in finished)
    assert all(label.id != source_label.id for label in finished)


async def test_move_card_to_board_preserves_done_status(
    nc_client: NextcloudClient, two_boards_with_stacks: tuple
):
    """A card marked done keeps its done state (not timestamp) across a move."""
    source_board_id, source_stack_id, target_board_id, target_stack_id = (
        two_boards_with_stacks
    )

    suffix = uuid.uuid4().hex[:8]
    card = await nc_client.deck.create_card(
        source_board_id, source_stack_id, f"Done card {suffix}"
    )
    done_ts = "2030-01-02T03:04:05+00:00"
    await nc_client.deck.update_card(
        board_id=source_board_id,
        stack_id=source_stack_id,
        card_id=card.id,
        done=done_ts,
    )
    before = await nc_client.deck.get_card(source_board_id, source_stack_id, card.id)
    assert before.done is not None, "Card should be done before the move"

    await nc_client.deck.move_card_to_board(
        source_board_id=source_board_id,
        source_stack_id=source_stack_id,
        card_id=card.id,
        target_board_id=target_board_id,
        target_stack_id=target_stack_id,
    )

    after = await nc_client.deck.get_card(target_board_id, target_stack_id, card.id)
    assert after.done is not None, "done status was cleared during the move"


async def test_move_card_to_board_preserves_archived_status(
    nc_client: NextcloudClient, two_boards_with_stacks: tuple
):
    """An archived card stays archived across a cross-board move."""
    source_board_id, source_stack_id, target_board_id, target_stack_id = (
        two_boards_with_stacks
    )

    suffix = uuid.uuid4().hex[:8]
    card = await nc_client.deck.create_card(
        source_board_id, source_stack_id, f"Archived card {suffix}"
    )
    await nc_client.deck.archive_card(source_board_id, source_stack_id, card.id)

    await nc_client.deck.move_card_to_board(
        source_board_id=source_board_id,
        source_stack_id=source_stack_id,
        card_id=card.id,
        target_board_id=target_board_id,
        target_stack_id=target_stack_id,
    )

    after = await nc_client.deck.get_card(target_board_id, target_stack_id, card.id)
    assert after.stackId == target_stack_id
    assert after.archived is True, "archived state was lost during the move"


async def test_move_card_to_board_preserves_done_and_archived(
    nc_client: NextcloudClient, two_boards_with_stacks: tuple
):
    """A done *and* archived card keeps both states across a cross-board move.

    This exercises the done-restore path (post-move done PUT + re-fetch) on an
    archived card, confirming get_card on the destination handles it.
    """
    source_board_id, source_stack_id, target_board_id, target_stack_id = (
        two_boards_with_stacks
    )

    suffix = uuid.uuid4().hex[:8]
    card = await nc_client.deck.create_card(
        source_board_id, source_stack_id, f"Done+archived {suffix}"
    )
    await nc_client.deck.update_card(
        board_id=source_board_id,
        stack_id=source_stack_id,
        card_id=card.id,
        done="2030-01-02T03:04:05+00:00",
    )
    await nc_client.deck.archive_card(source_board_id, source_stack_id, card.id)

    moved = await nc_client.deck.move_card_to_board(
        source_board_id=source_board_id,
        source_stack_id=source_stack_id,
        card_id=card.id,
        target_board_id=target_board_id,
        target_stack_id=target_stack_id,
    )
    assert moved.done is not None
    assert moved.archived is True

    after = await nc_client.deck.get_card(target_board_id, target_stack_id, card.id)
    assert after.stackId == target_stack_id
    assert after.done is not None, "done state was lost during the move"
    assert after.archived is True, "archived state was lost during the move"


async def test_move_card_to_board_preserves_assigned_users(
    nc_client: NextcloudClient, two_boards_with_stacks: tuple
):
    """Assigned users carry over a cross-board move (the route doesn't touch them).

    Deck's update route remaps labels on a board change but leaves the card's
    user assignments untouched, so an assignee that exists on both boards stays
    assigned.
    """
    source_board_id, source_stack_id, target_board_id, target_stack_id = (
        two_boards_with_stacks
    )

    suffix = uuid.uuid4().hex[:8]
    card = await nc_client.deck.create_card(
        source_board_id, source_stack_id, f"Assigned {suffix}"
    )
    # The test user owns both temporary boards, so it is a valid assignee on each
    await nc_client.deck.assign_user_to_card(
        source_board_id, source_stack_id, card.id, nc_client.username
    )
    before = await nc_client.deck.get_card(source_board_id, source_stack_id, card.id)
    assert before.assignedUsers, "Card should have an assignee before the move"

    await nc_client.deck.move_card_to_board(
        source_board_id=source_board_id,
        source_stack_id=source_stack_id,
        card_id=card.id,
        target_board_id=target_board_id,
        target_stack_id=target_stack_id,
    )

    after = await nc_client.deck.get_card(target_board_id, target_stack_id, card.id)
    assert after.assignedUsers, "Assigned users were dropped during the move"


async def test_move_card_to_board_rejects_stack_not_on_target_board(
    nc_client: NextcloudClient, two_boards_with_stacks: tuple
):
    """The move is rejected when target_stack_id is not on target_board_id."""
    source_board_id, source_stack_id, target_board_id, _target_stack_id = (
        two_boards_with_stacks
    )

    suffix = uuid.uuid4().hex[:8]
    card = await nc_client.deck.create_card(
        source_board_id, source_stack_id, f"Mismatch {suffix}"
    )

    with pytest.raises(ValueError, match="not a stack on target board"):
        await nc_client.deck.move_card_to_board(
            source_board_id=source_board_id,
            source_stack_id=source_stack_id,
            card_id=card.id,
            target_board_id=target_board_id,
            target_stack_id=source_stack_id,  # on the source board, not target
        )

    # The card stayed put
    still_there = await nc_client.deck.get_card(
        source_board_id, source_stack_id, card.id
    )
    assert still_there.stackId == source_stack_id


async def test_reorder_card_rejects_cross_board_target(
    nc_client: NextcloudClient, two_boards_with_stacks: tuple
):
    """reorder_card refuses a target stack on a different board."""
    source_board_id, source_stack_id, _target_board_id, target_stack_id = (
        two_boards_with_stacks
    )

    suffix = uuid.uuid4().hex[:8]
    card = await nc_client.deck.create_card(
        source_board_id, source_stack_id, f"No cross-board reorder {suffix}"
    )

    with pytest.raises(ValueError, match="move_card_to_board"):
        await nc_client.deck.reorder_card(
            board_id=source_board_id,
            stack_id=source_stack_id,
            card_id=card.id,
            order=0,
            target_stack_id=target_stack_id,  # belongs to the other board
        )

    # The card stayed put
    still_there = await nc_client.deck.get_card(
        source_board_id, source_stack_id, card.id
    )
    assert still_there.stackId == source_stack_id
