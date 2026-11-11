from typing import List

from nextcloud_client.models.deck import (
    CARD_URL_DESCRIPTION,
    DeckAttachment,
    DeckBoard,
    DeckCard,
    DeckCardSummary,
    DeckComment,
    DeckCommentSummary,
    DeckLabel,
    DeckStack,
)
from pydantic import BaseModel, Field

from .base import BaseResponse, StatusResponse


class ListBoardsResponse(BaseResponse):
    """Response model for listing deck boards."""

    boards: List[DeckBoard] = Field(description="List of deck boards")
    total: int = Field(description="Total number of boards")


class CreateBoardResponse(BaseResponse):
    """Response model for board creation."""

    id: int = Field(description="The created board ID")
    title: str = Field(description="The created board title")
    color: str = Field(description="The created board color")


class BoardOperationResponse(StatusResponse):
    """Response model for board operations like update/delete."""

    board_id: int = Field(description="ID of the affected board")


# Stack Response Models


class ListStacksResponse(BaseResponse):
    """Response model for listing deck stacks."""

    stacks: List[DeckStack] = Field(description="List of deck stacks")
    total: int = Field(description="Total number of stacks")


class StackOverview(BaseModel):
    """A stack plus its compact card rows, for the board-overview tool."""

    id: int = Field(description="Stack ID")
    title: str = Field(description="Stack title")
    order: int | None = Field(default=None, description="Stack sort order")
    card_count: int = Field(description="Number of cards returned for this stack")
    cards: list[DeckCardSummary] = Field(
        default_factory=list, description="Compact card rows in this stack"
    )


class BoardOverviewResponse(BaseResponse):
    """Compact whole-board snapshot: board → stacks → summary card rows.

    A single-call replacement for deck_get_board + deck_get_stacks that keeps
    the response small enough to fit the token budget on large boards.
    """

    board_id: int = Field(description="Board ID")
    title: str = Field(description="Board title")
    labels: list[str] = Field(
        default_factory=list, description="Board label titles (legend)"
    )
    stacks: list[StackOverview] = Field(
        default_factory=list, description="Stacks with compact card rows"
    )
    total_cards: int = Field(description="Total cards across all returned stacks")


class CreateStackResponse(BaseResponse):
    """Response model for stack creation."""

    id: int = Field(description="The created stack ID")
    title: str = Field(description="The created stack title")
    order: int = Field(description="The created stack order")


class StackOperationResponse(StatusResponse):
    """Response model for stack operations like update/delete."""

    stack_id: int = Field(description="ID of the affected stack")
    board_id: int = Field(description="ID of the board containing the stack")


# Card Response Models


class CreateCardResponse(BaseResponse):
    """Response model for card creation."""

    id: int = Field(description="The created card ID")
    title: str = Field(description="The created card title")
    stackId: int = Field(description="The stack ID the card belongs to")
    url: str | None = Field(default=None, description=CARD_URL_DESCRIPTION)


class CardOperationResponse(StatusResponse):
    """Response model for card operations like update/delete."""

    card_id: int = Field(description="ID of the affected card")
    stack_id: int = Field(description="ID of the stack containing the card")
    board_id: int = Field(description="ID of the board containing the card")
    labels: list[str] | None = Field(
        default=None,
        description=(
            "Label titles on the card after the operation, when relevant — "
            "e.g. after a cross-board move that remaps board-scoped labels to "
            "the destination board"
        ),
    )
    url: str | None = Field(default=None, description=CARD_URL_DESCRIPTION)


# Label Response Models


class CreateLabelResponse(BaseResponse):
    """Response model for label creation."""

    id: int = Field(description="The created label ID")
    title: str = Field(description="The created label title")
    color: str = Field(description="The created label color")


class ListCardsResponse(BaseResponse):
    """Response model for listing deck cards."""

    cards: list[DeckCard | DeckCardSummary] = Field(
        description="List of deck cards (summaries unless detail='full')"
    )
    total: int = Field(description="Total number of cards")


class ListLabelsResponse(BaseResponse):
    """Response model for listing deck labels."""

    labels: list[DeckLabel] = Field(description="List of deck labels")
    total: int = Field(description="Total number of labels")


class LabelOperationResponse(StatusResponse):
    """Response model for label operations like update/delete."""

    label_id: int = Field(description="ID of the affected label")
    board_id: int = Field(description="ID of the board containing the label")


# Comment Response Models


class ListCardCommentsResponse(BaseResponse):
    """Response model for listing card comments."""

    results: list[DeckComment | DeckCommentSummary] = Field(
        description="Card comments in this page (summaries unless detail='full')"
    )
    count: int = Field(
        description=(
            "Number of comments returned in this page (page size, not the "
            "server-side total — the Deck list endpoint does not expose a total)."
        )
    )


class CardCommentResponse(BaseResponse):
    """Response model returned when a card comment is created or updated.

    Covers the split case too, rather than a second model: a union return
    produces a messy tool output schema and forces every caller to branch on
    shape. The extra fields are additive -- an existing caller reading
    ``comment`` is unaffected.
    """

    comment: DeckComment = Field(
        description=(
            "The created or updated card comment. When the message was split, "
            "this is part 1 -- the parent of the remaining parts."
        )
    )
    parts: list[DeckComment] | None = Field(
        default=None,
        description=(
            "Every posted part in order, including part 1, when the message "
            "was split into more than one comment. Null when a single comment "
            "was posted."
        ),
    )
    part_count: int = Field(
        default=1,
        description=(
            "Number of comments actually posted (1 unless the message was split)."
        ),
    )


class CardCommentOperationResponse(StatusResponse):
    """Response model for card comment operations that don't return comment data (e.g. delete)."""

    card_id: int = Field(description="ID of the card the comment belongs to")
    comment_id: int = Field(description="ID of the affected comment")


# Attachment Response Models


class AttachFileResponse(BaseResponse):
    """Response model for attaching an existing Nextcloud file to a Deck card.

    The attachment is created by sharing the file with the card via the standard
    OCS Sharing API using ``shareType=12`` (``IShare::TYPE_DECK``). The returned
    ``attachment_id`` is the share ID, which is also the Deck attachment ID.
    """

    attachment_id: int = Field(
        description="ID of the created attachment (share ID)",
    )
    card_id: int = Field(description="ID of the card the file is attached to")
    path: str = Field(description="Path of the shared file in the user's Files")


class ListAttachmentsResponse(BaseResponse):
    """Response model for listing card attachments."""

    results: list[DeckAttachment] = Field(
        description="Attachments on the card (both type='file' and type='deck_file')"
    )
    count: int = Field(description="Number of attachments returned")


class AttachmentOperationResponse(StatusResponse):
    """Response model for attachment operations that don't return data (e.g. delete)."""

    card_id: int = Field(description="ID of the card the attachment belongs to")
    attachment_id: int = Field(description="ID of the affected attachment")
