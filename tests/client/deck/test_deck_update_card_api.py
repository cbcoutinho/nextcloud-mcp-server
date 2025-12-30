"""
Integration tests for DeckClient.update_card API behavior.

This test suite documents the behavior of our DeckClient.update_card method
and identifies bugs in how it handles partial updates.

FINDINGS:
The Deck API PUT endpoint is a FULL REPLACEMENT, not a partial update.
Fields not included in the request body are either:
- Required and cause 400 error (title, type, owner)
- Optional but get CLEARED if not sent (description)

Related issues:
- nextcloud-mcp-server #452: DeckClient.update_card always sets owner/type
- deck #3127: REST API Docs: missing parameter in "update cards"
- deck #4106: Provide a working example of API usage to update a cards details
"""

import httpx
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
    Test DeckClient.update_card() method behavior with various parameter combinations.

    These tests document the current buggy behavior where:
    1. Updating without title fails (400) - title is required but conditionally sent
    2. Updating with title clears description - description should be preserved
    """

    async def test_update_title_only_clears_description(
        self, nc_client, deck_test_card
    ):
        """
        BUG: Updating only the title clears the description.

        The Deck PUT API is a full replacement. Our client doesn't send
        description when not explicitly provided, so it gets cleared.
        """
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
        # BUG: Description was cleared instead of preserved
        assert updated.description == ""  # Should be "Original description"

    async def test_update_description_only_fails(self, nc_client, deck_test_card):
        """
        BUG: Updating only the description fails with 400.

        The Deck PUT API requires title, type, and owner.
        Our client doesn't send title when not explicitly provided.
        """
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await nc_client.deck.update_card(
                board_id=deck_test_card["board_id"],
                stack_id=deck_test_card["stack_id"],
                card_id=deck_test_card["card_id"],
                description="New description only",
            )

        assert exc_info.value.response.status_code == 400

    async def test_update_title_and_description(self, nc_client, deck_test_card):
        """Updating title and description together works correctly."""
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

    async def test_update_duedate_only_fails(self, nc_client, deck_test_card):
        """
        BUG: Updating only the duedate fails with 400.

        title is required but not sent when not explicitly provided.
        """
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await nc_client.deck.update_card(
                board_id=deck_test_card["board_id"],
                stack_id=deck_test_card["stack_id"],
                card_id=deck_test_card["card_id"],
                duedate="2025-12-31T23:59:59+00:00",
            )

        assert exc_info.value.response.status_code == 400

    async def test_update_archived_only_fails(self, nc_client, deck_test_card):
        """
        BUG: Updating only the archived status fails with 400.

        title is required but not sent when not explicitly provided.
        """
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await nc_client.deck.update_card(
                board_id=deck_test_card["board_id"],
                stack_id=deck_test_card["stack_id"],
                card_id=deck_test_card["card_id"],
                archived=True,
            )

        assert exc_info.value.response.status_code == 400

    async def test_update_preserves_type(self, nc_client, deck_test_card):
        """Type is correctly preserved (already always sent in current implementation)."""
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

    async def test_update_preserves_owner(self, nc_client, deck_test_card):
        """Owner is correctly preserved (already always sent in current implementation)."""
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

    async def test_update_order_only_fails(self, nc_client, deck_test_card):
        """
        BUG: Updating only the order fails with 400.

        title is required but not sent when not explicitly provided.
        """
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await nc_client.deck.update_card(
                board_id=deck_test_card["board_id"],
                stack_id=deck_test_card["stack_id"],
                card_id=deck_test_card["card_id"],
                order=1,
            )

        assert exc_info.value.response.status_code == 400
