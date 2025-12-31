"""
Integration tests for DeckClient.update_card API behavior.

These tests define the EXPECTED behavior for partial card updates:
- Only fields explicitly passed should be modified
- All other fields should be preserved unchanged

Related issues:
- nextcloud-mcp-server #452: DeckClient.update_card partial update bugs
- deck #3127: REST API Docs: missing parameter in "update cards"
- deck #4106: Provide a working example of API usage to update a cards details
"""

import pytest

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def deck_test_card(nc_client):
    """Create a board, stack, and card for testing, cleanup after."""
    board = await nc_client.deck.create_board("Test Update Card API", "FF0000")
    stack = await nc_client.deck.create_stack(board.id, "Test Stack", 1)
    card = await nc_client.deck.create_card(
        board.id,
        stack.id,
        "Original Title",
        type="plain",
        description="Original description",
    )

    yield {
        "board_id": board.id,
        "stack_id": stack.id,
        "card_id": card.id,
        "card": card,
    }

    # Cleanup
    await nc_client.deck.delete_board(board.id)


class TestDeckClientUpdateCard:
    """
    Test DeckClient.update_card() partial update behavior.

    Expected: Only explicitly provided fields are updated, all others preserved.
    """

    async def test_update_title_only_preserves_description(
        self, nc_client, deck_test_card
    ):
        """Updating only the title should preserve the description."""
        await nc_client.deck.update_card(
            board_id=deck_test_card["board_id"],
            stack_id=deck_test_card["stack_id"],
            card_id=deck_test_card["card_id"],
            title="New Title",
        )

        updated = await nc_client.deck.get_card(
            deck_test_card["board_id"],
            deck_test_card["stack_id"],
            deck_test_card["card_id"],
        )
        assert updated.title == "New Title"
        assert updated.description == "Original description"

    async def test_update_description_only(self, nc_client, deck_test_card):
        """Updating only the description should work and preserve other fields."""
        await nc_client.deck.update_card(
            board_id=deck_test_card["board_id"],
            stack_id=deck_test_card["stack_id"],
            card_id=deck_test_card["card_id"],
            description="New description only",
        )

        updated = await nc_client.deck.get_card(
            deck_test_card["board_id"],
            deck_test_card["stack_id"],
            deck_test_card["card_id"],
        )
        assert updated.title == "Original Title"
        assert updated.description == "New description only"

    async def test_update_title_and_description(self, nc_client, deck_test_card):
        """Updating title and description together should work."""
        await nc_client.deck.update_card(
            board_id=deck_test_card["board_id"],
            stack_id=deck_test_card["stack_id"],
            card_id=deck_test_card["card_id"],
            title="New Title",
            description="New description",
        )

        updated = await nc_client.deck.get_card(
            deck_test_card["board_id"],
            deck_test_card["stack_id"],
            deck_test_card["card_id"],
        )
        assert updated.title == "New Title"
        assert updated.description == "New description"

    async def test_update_duedate_only(self, nc_client, deck_test_card):
        """Updating only the duedate should work and preserve other fields."""
        await nc_client.deck.update_card(
            board_id=deck_test_card["board_id"],
            stack_id=deck_test_card["stack_id"],
            card_id=deck_test_card["card_id"],
            duedate="2025-12-31T23:59:59+00:00",
        )

        updated = await nc_client.deck.get_card(
            deck_test_card["board_id"],
            deck_test_card["stack_id"],
            deck_test_card["card_id"],
        )
        assert updated.title == "Original Title"
        assert updated.description == "Original description"
        assert updated.duedate is not None

    async def test_update_archived_only(self, nc_client, deck_test_card):
        """Updating only the archived status should work and preserve other fields."""
        await nc_client.deck.update_card(
            board_id=deck_test_card["board_id"],
            stack_id=deck_test_card["stack_id"],
            card_id=deck_test_card["card_id"],
            archived=True,
        )

        updated = await nc_client.deck.get_card(
            deck_test_card["board_id"],
            deck_test_card["stack_id"],
            deck_test_card["card_id"],
        )
        assert updated.title == "Original Title"
        assert updated.description == "Original description"
        assert updated.archived is True

    async def test_update_order_only(self, nc_client, deck_test_card):
        """Updating only the order should work and preserve other fields."""
        await nc_client.deck.update_card(
            board_id=deck_test_card["board_id"],
            stack_id=deck_test_card["stack_id"],
            card_id=deck_test_card["card_id"],
            order=99,
        )

        updated = await nc_client.deck.get_card(
            deck_test_card["board_id"],
            deck_test_card["stack_id"],
            deck_test_card["card_id"],
        )
        assert updated.title == "Original Title"
        assert updated.description == "Original description"
        assert updated.order == 99

    async def test_update_preserves_type(self, nc_client, deck_test_card):
        """Type should be preserved when not explicitly changed."""
        original = deck_test_card["card"]

        await nc_client.deck.update_card(
            board_id=deck_test_card["board_id"],
            stack_id=deck_test_card["stack_id"],
            card_id=deck_test_card["card_id"],
            title="Changed Title",
        )

        updated = await nc_client.deck.get_card(
            deck_test_card["board_id"],
            deck_test_card["stack_id"],
            deck_test_card["card_id"],
        )
        assert updated.type == original.type
        assert updated.description == "Original description"

    async def test_update_preserves_owner(self, nc_client, deck_test_card):
        """Owner should be preserved when not explicitly changed."""
        original = deck_test_card["card"]

        await nc_client.deck.update_card(
            board_id=deck_test_card["board_id"],
            stack_id=deck_test_card["stack_id"],
            card_id=deck_test_card["card_id"],
            title="Changed Title",
        )

        updated = await nc_client.deck.get_card(
            deck_test_card["board_id"],
            deck_test_card["stack_id"],
            deck_test_card["card_id"],
        )
        assert updated.owner == original.owner
        assert updated.description == "Original description"
