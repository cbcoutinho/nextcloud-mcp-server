import logging

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.deck import (
    AttachFileResponse,
    AttachmentOperationResponse,
    CardCommentOperationResponse,
    CardCommentResponse,
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
    ListAttachmentsResponse,
    ListBoardsResponse,
    ListCardCommentsResponse,
    ListCardsResponse,
    ListLabelsResponse,
    ListStacksResponse,
    StackOperationResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


def _validate_description_max_length(description_max_length: int | None) -> None:
    """Tool-layer guard: reject zero/negative truncation thresholds."""
    if description_max_length is not None and description_max_length <= 0:
        raise ValueError(
            f"description_max_length must be positive, got {description_max_length}"
        )


def _truncate_card_descriptions(
    cards: list[DeckCard], description_max_length: int | None
) -> None:
    """Truncate descriptions strictly longer than the limit; appends "…" so
    the truncated result is ``description_max_length + 1`` chars."""
    if description_max_length is None:
        return
    for card in cards:
        if card.description and len(card.description) > description_max_length:
            card.description = card.description[:description_max_length] + "…"


def _apply_board_filters(
    board: DeckBoard,
    *,
    include_acl: bool,
    include_users: bool,
    include_labels: bool,
) -> DeckBoard:
    """Drop board sub-fields the caller didn't request; returns the board."""
    if not include_acl:
        board.acl = []
    if not include_users:
        board.users = []
    if not include_labels:
        board.labels = []
    return board


def _apply_stack_filters(
    stack: DeckStack,
    *,
    include_cards: bool,
    include_archived_cards: bool,
    description_max_length: int | None,
) -> DeckStack:
    """Apply card-shaping filters to a single stack; returns the stack."""
    # Note: the upstream Deck API returns archived cards inline within
    # active stacks (the Deck UI filters them frontend-side). Defaulting
    # include_archived_cards to False mirrors that UI behavior — this is
    # the breaking change called out in the PR description.
    if not include_cards:
        stack.cards = None
    elif stack.cards:
        if not include_archived_cards:
            stack.cards = [c for c in stack.cards if not c.archived]
        _truncate_card_descriptions(stack.cards, description_max_length)
    return stack


def _apply_card_filters(
    cards: list[DeckCard],
    *,
    include_archived_cards: bool,
    description_max_length: int | None,
) -> list[DeckCard]:
    """Apply filters to a flat list of cards; returns the (possibly new) list."""
    if not include_archived_cards:
        cards = [c for c in cards if not c.archived]
    _truncate_card_descriptions(cards, description_max_length)
    return cards


# Card attachments — file shares ("Share from Files" picker in the Deck UI).
#
# Mechanism: a Deck card attachment of type="file" is just a Nextcloud share
# with shareType=12 (IShare::TYPE_DECK) and shareWith=<cardId>. The Deck UI
# fires this exact request — see Deck app's
# src/components/card/AttachmentList.vue:223-238 and lib/Service/FilesAppService.php.
# The file is NOT copied; the share row binds the file's existing path to the card.
_SHARE_TYPE_DECK = 12


def _resolve_note_path(notes_folder: str, category: str, title: str) -> str:
    """Reconstruct a note's file path from Notes API metadata.

    Notes are stored as ``<notes_folder>/<category>/<title>.md`` in the
    user's Files; ``<category>`` may be empty or nested (``"Foo/Bar"``).
    """
    parts = [notes_folder.strip("/")]
    if category:
        parts.append(category.strip("/"))
    parts.append(f"{title}.md")
    return "/" + "/".join(p for p in parts if p)


async def _resolve_note_attach_path(client, note_id: int) -> str:
    """Resolve a Notes-app note ID to its filesystem path for sharing.

    Hits the Notes API twice (settings + note metadata) and reconstructs
    the path. Encapsulates the camelCase key (``notesPath``, see
    ``models/notes.py:43``) so a typo there can't silently route to the
    default ``"Notes"`` folder for users who've configured a non-default
    notes location — that bug is exactly what this helper exists to make
    testable.
    """
    async with anyio.create_task_group() as tg:
        settings_holder: list[dict] = []
        note_holder: list[dict] = []

        async def _get_settings() -> None:
            settings_holder.append(await client.notes.get_settings())

        async def _get_note() -> None:
            note_holder.append(await client.notes.get_note(note_id))

        tg.start_soon(_get_settings)
        tg.start_soon(_get_note)

    settings = settings_holder[0]
    note = note_holder[0]
    notes_folder = settings.get("notesPath") or "Notes"
    return _resolve_note_path(
        notes_folder=notes_folder,
        category=note.get("category") or "",
        title=note["title"],
    )


def configure_deck_tools(mcp: FastMCP):
    """Configure Nextcloud Deck tools and resources for the MCP server."""

    # Resources
    @mcp.resource("nc://Deck/boards")
    async def deck_boards_resource():
        """List all Nextcloud Deck boards"""
        ctx: Context = mcp.get_context()
        await ctx.warning("This message is deprecated, use the deck_get_board instead")
        client = await get_client(ctx)
        boards = await client.deck.get_boards()
        return [board.model_dump() for board in boards]

    @mcp.resource("nc://Deck/boards/{board_id}")
    async def deck_board_resource(board_id: int):
        """Get details of a specific Nextcloud Deck board"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_board tool instead"
        )
        client = await get_client(ctx)
        board = await client.deck.get_board(board_id)
        return board.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/stacks")
    async def deck_stacks_resource(board_id: int):
        """List all stacks in a Nextcloud Deck board"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_stacks tool instead"
        )
        client = await get_client(ctx)
        stacks = await client.deck.get_stacks(board_id)
        return [stack.model_dump() for stack in stacks]

    @mcp.resource("nc://Deck/boards/{board_id}/stacks/{stack_id}")
    async def deck_stack_resource(board_id: int, stack_id: int):
        """Get details of a specific Nextcloud Deck stack"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_stack tool instead"
        )
        client = await get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        return stack.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/stacks/{stack_id}/cards")
    async def deck_cards_resource(board_id: int, stack_id: int):
        """List all cards in a Nextcloud Deck stack"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_cards tool instead"
        )
        client = await get_client(ctx)
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
        client = await get_client(ctx)
        card = await client.deck.get_card(board_id, stack_id, card_id)
        return card.model_dump()

    @mcp.resource("nc://Deck/boards/{board_id}/labels")
    async def deck_labels_resource(board_id: int):
        """List all labels in a Nextcloud Deck board"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_labels tool instead"
        )
        client = await get_client(ctx)
        board = await client.deck.get_board(board_id)
        return [label.model_dump() for label in (board.labels or [])]

    @mcp.resource("nc://Deck/boards/{board_id}/labels/{label_id}")
    async def deck_label_resource(board_id: int, label_id: int):
        """Get details of a specific Nextcloud Deck label"""
        ctx: Context = mcp.get_context()
        await ctx.warning(
            "This resource is deprecated, use the deck_get_label tool instead"
        )
        client = await get_client(ctx)
        label = await client.deck.get_label(board_id, label_id)
        return label.model_dump()

    # Read Tools (converted from resources)

    @mcp.tool(
        title="List Deck Boards",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_boards(ctx: Context) -> ListBoardsResponse:
        """Get all Nextcloud Deck boards"""
        client = await get_client(ctx)
        boards = await client.deck.get_boards()
        return ListBoardsResponse(boards=boards, total=len(boards))

    @mcp.tool(
        title="Get Deck Board",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_board(
        ctx: Context,
        board_id: int,
        include_acl: bool = True,
        include_users: bool = True,
        include_labels: bool = True,
    ) -> DeckBoard:
        """Get details of a specific Nextcloud Deck board.

        Args:
            board_id: The ID of the board
            include_acl: Include the board's ACL entries (default True). Set
                False to reduce response size when ACLs are not needed.
            include_users: Include the board's user list (default True). Set
                False to reduce response size when users are not needed.
            include_labels: Include the board's label definitions (default
                True). Set False to reduce response size; labels can still be
                retrieved via deck_get_labels.
        """
        client = await get_client(ctx)
        board = await client.deck.get_board(board_id)
        return _apply_board_filters(
            board,
            include_acl=include_acl,
            include_users=include_users,
            include_labels=include_labels,
        )

    @mcp.tool(
        title="List Deck Stacks",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_stacks(
        ctx: Context,
        board_id: int,
        include_cards: bool = True,
        include_archived_cards: bool = False,
        description_max_length: int | None = None,
    ) -> ListStacksResponse:
        """Get all stacks in a Nextcloud Deck board.

        Args:
            board_id: The ID of the board
            include_cards: Include cards inside each stack (default True). Set
                False for a lightweight stack listing; fetch cards separately
                via deck_get_cards.
            include_archived_cards: Include archived cards (default False).
                Only relevant when include_cards is True.
            description_max_length: If set, truncate each card's description
                to this many characters. Useful for keeping responses compact
                on boards with long card specs.
        """
        _validate_description_max_length(description_max_length)
        client = await get_client(ctx)
        stacks = await client.deck.get_stacks(board_id)
        stacks = [
            _apply_stack_filters(
                stack,
                include_cards=include_cards,
                include_archived_cards=include_archived_cards,
                description_max_length=description_max_length,
            )
            for stack in stacks
        ]
        return ListStacksResponse(stacks=stacks, total=len(stacks))

    @mcp.tool(
        title="Get Deck Stack",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_stack(
        ctx: Context,
        board_id: int,
        stack_id: int,
        include_cards: bool = True,
        include_archived_cards: bool = False,
        description_max_length: int | None = None,
    ) -> DeckStack:
        """Get details of a specific Nextcloud Deck stack.

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            include_cards: Include cards in the stack (default True).
            include_archived_cards: Include archived cards (default False).
                Only relevant when include_cards is True.
            description_max_length: If set, truncate each card's description
                to this many characters.
        """
        _validate_description_max_length(description_max_length)
        client = await get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        return _apply_stack_filters(
            stack,
            include_cards=include_cards,
            include_archived_cards=include_archived_cards,
            description_max_length=description_max_length,
        )

    @mcp.tool(
        title="List Archived Deck Stacks",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_archived_stacks(
        ctx: Context,
        board_id: int,
        description_max_length: int | None = None,
    ) -> ListStacksResponse:
        """List archived stacks (with their archived cards) for a Nextcloud
        Deck board.

        Use this to audit completed work that has been archived off the
        active board (e.g. cards moved through a "Done" stack and then
        archived via deck_archive_card). The shape mirrors deck_get_stacks.

        Cards are always included on the returned stacks (an archived stack
        without its cards would have no audit value); pass
        ``description_max_length`` if you need to keep the response compact.

        Args:
            board_id: The ID of the board
            description_max_length: If set, truncate each card's description
                to this many characters.
        """
        _validate_description_max_length(description_max_length)
        client = await get_client(ctx)
        stacks = await client.deck.get_archived_stacks(board_id)
        # All cards in archived stacks are themselves archived; route through
        # the same helper as the active-stack path so future filter additions
        # apply uniformly.
        stacks = [
            _apply_stack_filters(
                stack,
                include_cards=True,
                include_archived_cards=True,
                description_max_length=description_max_length,
            )
            for stack in stacks
        ]
        return ListStacksResponse(stacks=stacks, total=len(stacks))

    @mcp.tool(
        title="List Deck Cards",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_cards(
        ctx: Context,
        board_id: int,
        stack_id: int,
        include_archived_cards: bool = False,
        description_max_length: int | None = None,
    ) -> ListCardsResponse:
        """Get all cards in a Nextcloud Deck stack.

        Filtering is applied client-side after the API returns the full
        stack, so ``include_archived_cards=False`` and
        ``description_max_length`` reduce response size visible to the
        caller but not network bandwidth — network-wise this tool is
        equivalent to deck_get_stack(include_cards=True).

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            include_archived_cards: Include archived cards (default False).
                Archived cards can also be retrieved per-board via
                deck_get_archived_stacks.
            description_max_length: If set, truncate each card's description
                to this many characters.
        """
        _validate_description_max_length(description_max_length)
        client = await get_client(ctx)
        stack = await client.deck.get_stack(board_id, stack_id)
        cards = _apply_card_filters(
            stack.cards or [],
            include_archived_cards=include_archived_cards,
            description_max_length=description_max_length,
        )
        return ListCardsResponse(cards=cards, total=len(cards))

    @mcp.tool(
        title="Get Deck Card",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> DeckCard:
        """Get details of a specific Nextcloud Deck card"""
        client = await get_client(ctx)
        card = await client.deck.get_card(board_id, stack_id, card_id)
        return card

    @mcp.tool(
        title="List Deck Labels",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_labels(ctx: Context, board_id: int) -> ListLabelsResponse:
        """Get all labels in a Nextcloud Deck board"""
        client = await get_client(ctx)
        board = await client.deck.get_board(board_id)
        labels = board.labels or []
        return ListLabelsResponse(labels=labels, total=len(labels))

    @mcp.tool(
        title="Get Deck Label",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_label(ctx: Context, board_id: int, label_id: int) -> DeckLabel:
        """Get details of a specific Nextcloud Deck label"""
        client = await get_client(ctx)
        label = await client.deck.get_label(board_id, label_id)
        return label

    # Create/Update/Delete Tools

    @mcp.tool(
        title="Create Deck Board",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_board(
        ctx: Context, title: str, color: str
    ) -> CreateBoardResponse:
        """Create a new Nextcloud Deck board

        Args:
            title: The title of the new board
            color: The hexadecimal color of the new board (e.g. FF0000)
        """
        client = await get_client(ctx)
        board = await client.deck.create_board(title, color)
        return CreateBoardResponse(id=board.id, title=board.title, color=board.color)

    # Stack Tools

    @mcp.tool(
        title="Create Deck Stack",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_stack(
        ctx: Context, board_id: int, title: str, order: int
    ) -> CreateStackResponse:
        """Create a new stack in a Nextcloud Deck board

        Args:
            board_id: The ID of the board
            title: The title of the new stack
            order: Order for sorting the stacks
        """
        client = await get_client(ctx)
        stack = await client.deck.create_stack(board_id, title, order)
        return CreateStackResponse(id=stack.id, title=stack.title, order=stack.order)

    @mcp.tool(
        title="Update Deck Stack",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_update_stack(
        ctx: Context,
        board_id: int,
        stack_id: int,
        title: str | None = None,
        order: int | None = None,
    ) -> StackOperationResponse:
        """Update a Nextcloud Deck stack

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            title: New title for the stack
            order: New order for the stack
        """
        client = await get_client(ctx)
        await client.deck.update_stack(board_id, stack_id, title, order)
        return StackOperationResponse(
            success=True,
            message="Stack updated successfully",
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Delete Deck Stack",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_stack(
        ctx: Context, board_id: int, stack_id: int
    ) -> StackOperationResponse:
        """Delete a Nextcloud Deck stack

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
        """
        client = await get_client(ctx)
        await client.deck.delete_stack(board_id, stack_id)
        return StackOperationResponse(
            success=True,
            message="Stack deleted successfully",
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card Tools
    @mcp.tool(
        title="Create Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        title: str,
        type: str = "plain",
        order: int = 999,
        description: str | None = None,
        duedate: str | None = None,
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
        client = await get_client(ctx)
        card = await client.deck.create_card(
            board_id, stack_id, title, type, order, description, duedate
        )
        return CreateCardResponse(
            id=card.id,
            title=card.title,
            description=card.description,
            stackId=card.stackId,
        )

    @mcp.tool(
        title="Update Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_update_card(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        title: str | None = None,
        description: str | None = None,
        type: str | None = None,
        owner: str | None = None,
        order: int | None = None,
        duedate: str | None = None,
        archived: bool | None = None,
        done: str | None = None,
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
        client = await get_client(ctx)
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

    @mcp.tool(
        title="Delete Deck Card",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Delete a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = await get_client(ctx)
        await client.deck.delete_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card deleted successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Archive Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_archive_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Archive a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = await get_client(ctx)
        await client.deck.archive_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card archived successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Unarchive Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_unarchive_card(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> CardOperationResponse:
        """Unarchive a Nextcloud Deck card

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = await get_client(ctx)
        await client.deck.unarchive_card(board_id, stack_id, card_id)
        return CardOperationResponse(
            success=True,
            message="Card unarchived successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Reorder/Move Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
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
        client = await get_client(ctx)
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
    @mcp.tool(
        title="Create Deck Label",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_label(
        ctx: Context, board_id: int, title: str, color: str
    ) -> CreateLabelResponse:
        """Create a new label in a Nextcloud Deck board

        Args:
            board_id: The ID of the board
            title: The title of the new label
            color: The color of the new label (hex format without #)
        """
        client = await get_client(ctx)
        label = await client.deck.create_label(board_id, title, color)
        return CreateLabelResponse(id=label.id, title=label.title, color=label.color)

    @mcp.tool(
        title="Update Deck Label",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_update_label(
        ctx: Context,
        board_id: int,
        label_id: int,
        title: str | None = None,
        color: str | None = None,
    ) -> LabelOperationResponse:
        """Update a Nextcloud Deck label

        Args:
            board_id: The ID of the board
            label_id: The ID of the label
            title: New title for the label
            color: New color for the label (hex format without #)
        """
        client = await get_client(ctx)
        await client.deck.update_label(board_id, label_id, title, color)
        return LabelOperationResponse(
            success=True,
            message="Label updated successfully",
            label_id=label_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Delete Deck Label",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_label(
        ctx: Context, board_id: int, label_id: int
    ) -> LabelOperationResponse:
        """Delete a Nextcloud Deck label

        Args:
            board_id: The ID of the board
            label_id: The ID of the label
        """
        client = await get_client(ctx)
        await client.deck.delete_label(board_id, label_id)
        return LabelOperationResponse(
            success=True,
            message="Label deleted successfully",
            label_id=label_id,
            board_id=board_id,
        )

    # Card-Label Assignment Tools
    @mcp.tool(
        title="Assign Label to Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
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
        client = await get_client(ctx)
        await client.deck.assign_label_to_card(board_id, stack_id, card_id, label_id)
        return CardOperationResponse(
            success=True,
            message="Label assigned to card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Remove Label from Deck Card",
        annotations=ToolAnnotations(idempotentHint=True, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
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
        client = await get_client(ctx)
        await client.deck.remove_label_from_card(board_id, stack_id, card_id, label_id)
        return CardOperationResponse(
            success=True,
            message="Label removed from card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card-User Assignment Tools
    @mcp.tool(
        title="Assign User to Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
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
        client = await get_client(ctx)
        await client.deck.assign_user_to_card(board_id, stack_id, card_id, user_id)
        return CardOperationResponse(
            success=True,
            message="User assigned to card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    @mcp.tool(
        title="Unassign User from Deck Card",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
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
        client = await get_client(ctx)
        await client.deck.unassign_user_from_card(board_id, stack_id, card_id, user_id)
        return CardOperationResponse(
            success=True,
            message="User unassigned from card successfully",
            card_id=card_id,
            stack_id=stack_id,
            board_id=board_id,
        )

    # Card Comment Tools

    _COMMENT_MAX_LENGTH = 1000

    def _validate_comment_message(message: str) -> None:
        if len(message) > _COMMENT_MAX_LENGTH:
            raise ValueError(
                f"Comment message too long: {len(message)} characters "
                f"(max {_COMMENT_MAX_LENGTH})"
            )

    @mcp.tool(
        title="List Deck Card Comments",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_get_card_comments(
        ctx: Context, card_id: int, limit: int = 20, offset: int = 0
    ) -> ListCardCommentsResponse:
        """List comments on a Nextcloud Deck card

        Args:
            card_id: The ID of the card
            limit: Maximum number of comments to return (default 20, max 200)
            offset: Pagination offset (default 0)
        """
        client = await get_client(ctx)
        comments = await client.deck.get_comments(card_id, limit=limit, offset=offset)
        return ListCardCommentsResponse(results=comments, count=len(comments))

    @mcp.tool(
        title="Create Deck Card Comment",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_create_card_comment(
        ctx: Context,
        card_id: int,
        message: str,
        parent_id: int | None = None,
    ) -> CardCommentResponse:
        """Create a comment on a Nextcloud Deck card

        Supports @-mentions (e.g. "@alice"). Pass parent_id to reply to an
        existing comment on the same card. Message is limited to 1000 characters.

        Args:
            card_id: The ID of the card to comment on
            message: The comment text (max 1000 characters)
            parent_id: Optional ID of a parent comment to reply to
        """
        _validate_comment_message(message)
        client = await get_client(ctx)
        comment = await client.deck.create_comment(
            card_id, message, parent_id=parent_id
        )
        return CardCommentResponse(comment=comment)

    @mcp.tool(
        title="Update Deck Card Comment",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_update_card_comment(
        ctx: Context, card_id: int, comment_id: int, message: str
    ) -> CardCommentResponse:
        """Update a Nextcloud Deck card comment

        Only the comment's author can update it; the server returns 403 otherwise.

        Args:
            card_id: The ID of the card the comment belongs to
            comment_id: The ID of the comment to update
            message: The new comment text (max 1000 characters)
        """
        _validate_comment_message(message)
        client = await get_client(ctx)
        comment = await client.deck.update_comment(card_id, comment_id, message)
        return CardCommentResponse(comment=comment)

    @mcp.tool(
        title="Delete Deck Card Comment",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_card_comment(
        ctx: Context, card_id: int, comment_id: int
    ) -> CardCommentOperationResponse:
        """Delete a Nextcloud Deck card comment

        Only the comment's author can delete it; the server returns 403 otherwise.

        Args:
            card_id: The ID of the card the comment belongs to
            comment_id: The ID of the comment to delete
        """
        client = await get_client(ctx)
        await client.deck.delete_comment(card_id, comment_id)
        return CardCommentOperationResponse(
            success=True,
            message="Comment deleted successfully",
            card_id=card_id,
            comment_id=comment_id,
        )

    @mcp.tool(
        title="Attach File to Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write", "files.read")
    @instrument_tool
    async def deck_attach_file(
        ctx: Context, card_id: int, path: str
    ) -> AttachFileResponse:
        """Attach an existing Nextcloud file to a Deck card without copying.

        Creates a share of ``path`` with the card (``shareType=12``,
        ``shareWith=<card_id>``). The file stays in its original location;
        clicking the attachment in the Deck UI opens the file in place.

        Generic over the user's Files: works for any file the caller can
        read — markdown notes, PDFs, images, spreadsheets, etc. Use
        :func:`deck_attach_note` if you have a Notes-app note ID and want
        the path resolved automatically. Calling twice with the same
        ``path`` creates two distinct shares — caller is responsible for
        de-duping.

        Args:
            card_id: The ID of the Deck card to attach to
            path: Path to the file in the user's Nextcloud Files (must start
                with "/", e.g. "/Documents/spec.pdf" or "/Notes/My Note.md")
        """
        if not path.startswith("/"):
            raise ValueError(
                f"path must start with '/', got: {path!r} "
                "(paths are relative to the user's Files root)"
            )
        client = await get_client(ctx)
        share = await client.sharing.create_share(
            path=path,
            share_with=str(card_id),
            share_type=_SHARE_TYPE_DECK,
            permissions=1,
        )
        return AttachFileResponse(
            attachment_id=int(share["id"]),
            card_id=card_id,
            path=path,
        )

    @mcp.tool(
        title="Attach Note to Deck Card",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("deck.write", "files.read", "notes.read")
    @instrument_tool
    async def deck_attach_note(
        ctx: Context, card_id: int, note_id: int
    ) -> AttachFileResponse:
        """Attach a Nextcloud Note to a Deck card without copying.

        Convenience wrapper: looks up the note's filesystem path from the
        Notes app settings + note metadata, then shares the file with the
        card (same mechanism as :func:`deck_attach_file`). The note remains
        editable in the Notes app; the card just shows a clickable link to
        it.

        Path is reconstructed as ``<notes_folder>/<category>/<title>.md``.
        If the note's title contains characters that the Notes app sanitises
        differently (rare), use :func:`deck_attach_file` with the explicit
        path instead.

        Args:
            card_id: The ID of the Deck card to attach to
            note_id: The ID of the Note to attach
        """
        client = await get_client(ctx)
        path = await _resolve_note_attach_path(client, note_id)
        share = await client.sharing.create_share(
            path=path,
            share_with=str(card_id),
            share_type=_SHARE_TYPE_DECK,
            permissions=1,
        )
        return AttachFileResponse(
            attachment_id=int(share["id"]),
            card_id=card_id,
            path=path,
        )

    @mcp.tool(
        title="List Deck Card Attachments",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("deck.read")
    @instrument_tool
    async def deck_list_attachments(
        ctx: Context, board_id: int, stack_id: int, card_id: int
    ) -> ListAttachmentsResponse:
        """List attachments on a Nextcloud Deck card.

        Returns both shared-file attachments (``type="file"``, created via
        :func:`deck_attach_file` / :func:`deck_attach_note`) and uploaded
        binary attachments (``type="deck_file"``).

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
        """
        client = await get_client(ctx)
        attachments = await client.deck.get_attachments(board_id, stack_id, card_id)
        return ListAttachmentsResponse(results=attachments, count=len(attachments))

    @mcp.tool(
        title="Delete Deck Card Attachment",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("deck.write")
    @instrument_tool
    async def deck_delete_attachment(
        ctx: Context,
        board_id: int,
        stack_id: int,
        card_id: int,
        attachment_id: int,
    ) -> AttachmentOperationResponse:
        """Delete an attachment from a Nextcloud Deck card.

        For ``type="file"`` attachments this removes the share linking the
        file to the card; the underlying file in the user's Files is left
        untouched. For ``type="deck_file"`` blobs the binary is deleted from
        Deck's storage.

        Args:
            board_id: The ID of the board
            stack_id: The ID of the stack
            card_id: The ID of the card
            attachment_id: The ID of the attachment to delete
        """
        client = await get_client(ctx)
        await client.deck.delete_attachment(board_id, stack_id, card_id, attachment_id)
        return AttachmentOperationResponse(
            success=True,
            message="Attachment deleted successfully",
            card_id=card_id,
            attachment_id=attachment_id,
        )
