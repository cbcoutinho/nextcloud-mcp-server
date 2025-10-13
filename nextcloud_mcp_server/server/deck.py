import logging
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.deck import (
    CardOperationResponse,
    CreateBoardResponse,
    CreateCardResponse,
    CreateLabelResponse,
    CreateStackResponse,
    DeckBoard,
    DeckCard,
    DeckLabel,
    DeckStack,
    LabelOperationResponse,
    StackOperationResponse,
)

logger = logging.getLogger(__name__)


def configure_deck_tools(mcp: FastMCP):
    """Configure Nextcloud Deck tools and resources for the MCP server."""

    # Resources
    @mcp.resource("nc://Deck/boards")
    async def deck_boards_resource():
        """List all Nextcloud Deck boards"""
        ctx: Context = mcp.get_context()
        await ctx.warning("This message is deprecated, use the deck_get_board instead")
        client = get_client(ctx)
        boards = await client.deck.get_boards()
        return [board.model_dump() for board in boards]

    @mcp.resource("nc://Deck/boards/{board_id}")
    async def deck_board_resource(board_id: int):
        """Get details of a specific Nextcloud Deck board"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_board tool instead"
        )
        client = get_client(ctx)
        board = await client.deck.get_board(board_id)
        return board.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/stacks")
    async def deck_stacks_resource(board_id: int):
        """List all stacks in a Nextcloud Deck board"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_stacks tool instead"
        )
        client = get_client(ctx)
        stacks = await client.deck.get_stacks(board_id)
        return [stack.model_dump() for stack in stacks]

    @mcp.resource("nc://Deck/boards/{board_id}/stacks/{stack_id}")
    async def deck_stack_resource(board_id: int, stack_id: int):
        """Get details of a specific Nextcloud Deck stack"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_stack tool instead"
        )
        client = get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        return stack.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/stacks/{stack_id}/cards")
    async def deck_cards_resource(board_id: int, stack_id: int):
        """List all cards in a Nextcloud Deck stack"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_cards tool instead"
        )
        client = get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        if stack.cards:
            return [card.model_dump() for card in stack.cards]
        return []

    @mcp.resource("nc://Deck/boards/{board_id}/stacks/{stack_id}/cards/{card_id}")
    async def deck_card_resource(board_id: int, stack_id: int, card_id: int):
        """Get details of a specific Nextcloud Deck card"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_card tool instead"
        )
        client = get_client(ctx)
        card = await client.deck.get_card(board_id, stack_id, card_id)
        return card.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/labels")
    async def deck_labels_resource(board_id: int):
        """List all labels in a Nextcloud Deck board"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_labels tool instead"
        )
        client = get_client(ctx)
        board = await client.deck.get_board(board_id)
        return [label.model_dump() for label in board.labels]

    @mcp.resource("nc://Deck/boards/{board_id}/labels/{label_id}")
    async def deck_label_resource(board_id: int, label_id: int):
        """Get details of a specific Nextcloud Deck label"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_label tool instead"
        )
        client = get_client(ctx)
        label = await client.deck.get_label(board_id, label_id)
        return label.model_dump()

    # Read Tools (converted from resources)

    @mcp.tool()
    async def deck_get_boards(ctx: Context) -> list[DeckBoard]:
        """Get all Nextcloud Deck boards"""
        client = get_client(ctx)
        boards = await client.deck.get_boards()
        return boards

    @mcp.tool()
    async def deck_get_board(ctx: Context, board_id: int) -> DeckBoard:
        """Get details of a specific Nextcloud Deck board"""
        client = get_client(ctx)
        board = await client.deck.get_board(board_id)
        return board

    @mcp.tool()
    async def deck_get_stacks(ctx: Context, board_id: int) -> list[DeckStack]:
        """Get all stacks in a Nextcloud Deck board"""
        client = get_client(ctx)
        stacks = await client.deck.get_stacks(board_id)
        return stacks

    @mcp.tool()
    async def deck_get_stack(ctx: Context, board_id: int, stack_id: int) -> DeckStack:
        """Get details of a specific Nextcloud Deck stack"""
        client = get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        return stack

    @mcp.tool()
    async def deck_get_cards(
        ctx: Context, board_id: int, stack_id: int
    ) -> list[DeckCard]:
        """Get all cards in a Nextcloud Deck stack"""
        client = get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        if stack.cards:
            return stack.cards
        return []

    @mcp.tool()
    async def deck_get_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> DeckCard:
        """Get details of a specific Nextcloud Deck card"""
        client = get_client(ctx)
        card = await client.deck.get_card(board_id, stack_id, card_id)
        return card

    @mcp.tool()
    async def deck_get_labels(ctx: Context, board_id: int) -> list[DeckLabel]:
        """Get all labels in a Nextcloud Deck board"""
        client = get_client(ctx)
        board = await client.deck.get_board(board_id)
        return board.labels

    @mcp.tool()
    async def deck_get_label(ctx: Context, board_id: int, label_id: int) -> DeckLabel:
        """Get details of a specific Nextcloud Deck label"""
        client = get_client(ctx)
        label = await client.deck.get_label(board_id, label_id)
        return label

    # Create/Update/Delete Tools

    @mcp.tool()
    async def deck_create_board(
        ctx: Context, title: str, color: str
    ) -> CreateBoardResponse:
        """Create a new Nextcloud Deck board

        Args:
            title: The title of the new board
            color: The hexadecimal color of the new board (e.g. FF0000)
        """
        client = get_client(ctx)
        board = await client.deck.create_board(title, color)
        return CreateBoardResponse(id=board.id, title=board.title, color=board.color)

    # Stack Tools

    @mcp.tool()
    async def deck_create_stack(
        ctx: Context, board_id: int, title: str, order: int
    ) -> CreateStackResponse:
        """Create a new stack in a Nextcloud Deck board

        Args:
            board_id: The ID of the board
            title: The title of the new stack
            order: Order for sorting the stacks
        """
        client = get_client(ctx)
        stack = await client.deck.create_stack(board_id, title, order)
        return CreateStackResponse(id=stack.id, title=stack.title, order=stack.order)

    @mcp.tool()
    async def deck_update_stack(
        ctx: Context,
        board_id: int,
        stack_id: int,
        title: Optional[str] = None,
        order: Optional[int] = None,
    ) -> StackOperationResponse:
        """Update a Nextcloud Deck stack

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            title: New title for the stack
            order: New order for the stack
        """
        client = get_client(ctx)
        await client.deck.update_stack(board_id, stack_id, title, order)
        return StackOperationResponse(
            success=True,
            message="Stack updated successfully",
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool()
    async def deck_delete_stack(
        ctx: Context, board_id: int, stack_id: int
    ) -> StackOperationResponse:
        """Delete a Nextcloud Deck stack

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
        """
        client = get_client(ctx)
        await client.deck.delete_stack(board_id, stack_id)
        return StackOperationResponse(
            success=True,
            message="Stack deleted successfully",
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card Tools
    @mcp.tool()
    async def deck_create_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        title: str,
        type: str = "plain",
        order: int = 999,
        description: Optional[str] = None,
        duedate: Optional[str] = None,
    ) -> CreateCardResponse:
        """Create a new card in a Nextcloud Deck stack

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            title: The title of the new card
            type: Type of the card (default: plain)
            order: Order for sorting the cards
            description: Description of the card
            duedate: Due date of the card (ISO-8601 format)
        """
        client = get_client(ctx)
        card = await client.deck.create_card(
            board_id, stack_id, title, type, order, description, duedate
        )
        return CreateCardResponse(
            id=card.id,
            title=card.title,
            description=card.description,
            stackId=card.stackId,
        )

    @mcp.tool()
    async def deck_update_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        type: Optional[str] = None,
        owner: Optional[str] = None,
        order: Optional[int] = None,
        duedate: Optional[str] = None,
        archived: Optional[bool] = None,
        done: Optional[str] = None,
    ) -> CardOperationResponse:
        """Update a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            title: New title for the card
            description: New description for the card
            type: New type for the card
            owner: New owner for the card
            order: New order for the card
            duedate: New due date for the card (ISO-8601 format)
            archived: Whether the card should be archived
            done: Completion date for the card (ISO-8601 format)
        """
        client = get_client(ctx)
        await client.deck.update_card(
            board_id,
            stack_id,
            card_id,
            title,
            description,
            type,
            owner,
            order,
            duedate,
            archived,
            done,
        )
        return CardOperationResponse(
            success=True,
            message="Card updated successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool()
    async def deck_delete_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Delete a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = get_client(ctx)
        await client.deck.delete_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card deleted successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool()
    async def deck_archive_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Archive a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = get_client(ctx)
        await client.deck.archive_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card archived successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool()
    async def deck_unarchive_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Unarchive a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = get_client(ctx)
        await client.deck.unarchive_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card unarchived successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool()
    async def deck_reorder_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        order: int,
        target_stack_id: int,
    ) -> CardOperationResponse:
        """Reorder/move a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the current stack
            card_id: The ID of the card
            order: New position in the target stack
            target_stack_id: The ID of the target stack
        """
        client = get_client(ctx)
        await client.deck.reorder_card(
            board_id, stack_id, card_id, order, target_stack_id
        )
        return CardOperationResponse(
            success=True,
            message="Card reordered successfully",
            card_id=card_id,
            stack_id=target_stack_id,
            board_id=board_id,
        )

    # Label Tools
    @mcp.tool()
    async def deck_create_label(
        ctx: Context, board_id: int, title: str, color: str
    ) -> CreateLabelResponse:
        """Create a new label in a Nextcloud Deck board

        Args:
            board_id: The ID of the board
            title: The title of the new label
            color: The color of the new label (hex format without #)
        """
        client = get_client(ctx)
        label = await client.deck.create_label(board_id, title, color)
        return CreateLabelResponse(id=label.id, title=label.title, color=label.color)

    @mcp.tool()
    async def deck_update_label(
        ctx: Context,
        board_id: int,
        label_id: int,
        title: Optional[str] = None,
        color: Optional[str] = None,
    ) -> LabelOperationResponse:
        """Update a Nextcloud Deck label

        Args:
            board_id: The ID of the board
            label_id: The ID of the label
            title: New title for the label
            color: New color for the label (hex format without #)
        """
        client = get_client(ctx)
        await client.deck.update_label(board_id, label_id, title, color)
        return LabelOperationResponse(
            success=True,
            message="Label updated successfully",
            label_id=label_id,
            board_id=board_id,
        )

    @mcp.tool()
    async def deck_delete_label(
        ctx: Context, board_id: int, label_id: int
    ) -> LabelOperationResponse:
        """Delete a Nextcloud Deck label

        Args:
            board_id: The ID of the board
            label_id: The ID of the label
        """
        client = get_client(ctx)
        await client.deck.delete_label(board_id, label_id)
        return LabelOperationResponse(
            success=True,
            message="Label deleted successfully",
            label_id=label_id,
            board_id=board_id,
        )

    # Card-Label Assignment Tools
    @mcp.tool()
    async def deck_assign_label_to_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int, label_id: int
    ) -> CardOperationResponse:
        """Assign a label to a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            label_id: The ID of the label to assign
        """
        client = get_client(ctx)
        await client.deck.assign_label_to_card(board_id, stack_id, card_id, label_id)
        return CardOperationResponse(
            success=True,
            message="Label assigned to card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool()
    async def deck_remove_label_from_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int, label_id: int
    ) -> CardOperationResponse:
        """Remove a label from a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            label_id: The ID of the label to remove
        """
        client = get_client(ctx)
        await client.deck.remove_label_from_card(board_id, stack_id, card_id, label_id)
        return CardOperationResponse(
            success=True,
            message="Label removed from card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card-User Assignment Tools
    @mcp.tool()
    async def deck_assign_user_to_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int, user_id: str
    ) -> CardOperationResponse:
        """Assign a user to a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            user_id: The user ID to assign
        """
        client = get_client(ctx)
        await client.deck.assign_user_to_card(board_id, stack_id, card_id, user_id)
        return CardOperationResponse(
            success=True,
            message="User assigned to card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool()
    async def deck_unassign_user_from_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int, user_id: str
    ) -> CardOperationResponse:
        """Unassign a user from a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            user_id: The user ID to unassign
        """
        client = get_client(ctx)
        await client.deck.unassign_user_from_card(board_id, stack_id, card_id, user_id)
        return CardOperationResponse(
            success=True,
            message="User unassigned from card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )
