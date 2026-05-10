from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator

from .base import BaseResponse, StatusResponse


class DeckUser(BaseModel):
    primaryKey: str
    uid: str
    displayname: str


class DeckPermissions(BaseModel):
    PERMISSION_READ: bool
    PERMISSION_EDIT: bool
    PERMISSION_MANAGE: bool
    PERMISSION_SHARE: bool


class DeckLabel(BaseModel):
    id: int
    title: str
    color: str
    boardId: Optional[int] = None
    cardId: Optional[int] = None


class DeckACL(BaseModel):
    id: int
    participant: DeckUser
    type: int
    boardId: int
    permissionEdit: bool
    permissionShare: bool
    permissionManage: bool
    owner: bool


class DeckBoardSettings(BaseModel):
    calendar: bool
    cardDetailsInModal: Optional[bool] = Field(default=None, alias="cardDetailsInModal")
    cardIdBadge: Optional[bool] = Field(default=None, alias="cardIdBadge")
    groupLimit: Optional[List[Dict[str, str]]] = Field(default=None, alias="groupLimit")
    notify_due: Optional[str] = Field(default=None, alias="notify-due")


class DeckBoard(BaseModel):
    id: int
    title: str
    owner: DeckUser
    color: str
    archived: bool
    labels: List[DeckLabel]
    acl: List[DeckACL]
    permissions: DeckPermissions
    users: List[DeckUser]
    deletedAt: int
    lastModified: Optional[int] = None
    settings: Optional[DeckBoardSettings] = None
    etag: Optional[str] = Field(default=None, alias="ETag")

    @field_validator("settings", mode="before")
    @classmethod
    def validate_settings(cls, v):
        # Handle case where API returns empty array instead of dict/null
        if isinstance(v, list) and len(v) == 0:
            return None
        return v


class DeckAssignedUser(BaseModel):
    id: int
    participant: DeckUser
    cardId: int
    type: int


class DeckCard(BaseModel):
    id: int
    title: str
    stackId: int
    type: str
    order: int
    archived: bool
    owner: Union[str, DeckUser]  # Can be either string or user object
    description: Optional[str] = None
    duedate: Optional[datetime] = None
    done: Optional[datetime] = None
    lastModified: Optional[int] = None
    createdAt: Optional[int] = None
    labels: Optional[List[DeckLabel]] = None
    assignedUsers: Optional[List[Union[DeckUser, DeckAssignedUser]]] = None
    attachments: Optional[List[Any]] = None  # Define a proper Attachment model later
    attachmentCount: Optional[int] = None
    deletedAt: Optional[int] = None
    commentsUnread: Optional[int] = None
    overdue: Optional[int] = None
    etag: Optional[str] = Field(default=None, alias="ETag")

    @field_validator("owner", mode="before")
    @classmethod
    def validate_owner(cls, v):
        # Handle case where API returns user object instead of string
        if isinstance(v, dict):
            return v.get("uid", v.get("primaryKey", str(v)))
        return v

    @field_validator("assignedUsers", mode="before")
    @classmethod
    def validate_assigned_users(cls, v):
        # Handle different formats of assigned users from the API
        if not v:
            return v

        validated_users = []
        for user in v:
            if isinstance(user, dict):
                # Check if it's an assignment object with participant
                if "participant" in user:
                    validated_users.append(user)
                # Check if it's a direct user object
                elif "uid" in user or "primaryKey" in user:
                    validated_users.append(user)
            else:
                validated_users.append(user)

        return validated_users


class DeckStack(BaseModel):
    id: int
    title: str
    boardId: int
    order: int
    deletedAt: int
    lastModified: Optional[int] = None
    cards: Optional[List[DeckCard]] = None
    etag: Optional[str] = Field(default=None, alias="ETag")


class DeckAttachmentExtendedData(BaseModel):
    filesize: int
    mimetype: str
    info: Dict[str, str]
    # Populated for type="file" (Files share) attachments via FilesAppService.
    path: str | None = None
    fileid: int | None = None
    hasPreview: bool | None = None
    permissions: int | None = None


class DeckAttachment(BaseModel):
    id: int
    cardId: int
    type: str
    data: str
    lastModified: int
    createdAt: int
    createdBy: str
    deletedAt: int
    extendedData: DeckAttachmentExtendedData


class DeckComment(BaseModel):
    id: int
    objectId: int
    message: str
    actorId: str
    actorType: str
    actorDisplayName: str
    creationDateTime: datetime
    mentions: List[Dict[str, str]]
    replyTo: Optional[Any] = None  # Self-referencing, handle later if needed


class DeckSession(BaseModel):
    token: str


class DeckConfig(BaseModel):
    calendar: bool
    cardDetailsInModal: bool
    cardIdBadge: bool
    groupLimit: Optional[List[Dict[str, str]]] = None


# Response Models for MCP Tools


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
    description: Optional[str] = Field(description="The created card description")
    stackId: int = Field(description="The stack ID the card belongs to")


class CardOperationResponse(StatusResponse):
    """Response model for card operations like update/delete."""

    card_id: int = Field(description="ID of the affected card")
    stack_id: int = Field(description="ID of the stack containing the card")
    board_id: int = Field(description="ID of the board containing the card")


# Label Response Models


class CreateLabelResponse(BaseResponse):
    """Response model for label creation."""

    id: int = Field(description="The created label ID")
    title: str = Field(description="The created label title")
    color: str = Field(description="The created label color")


class ListCardsResponse(BaseResponse):
    """Response model for listing deck cards."""

    cards: list[DeckCard] = Field(description="List of deck cards")
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

    results: list[DeckComment] = Field(description="Card comments in this page")
    count: int = Field(
        description=(
            "Number of comments returned in this page (page size, not the "
            "server-side total — the Deck list endpoint does not expose a total)."
        )
    )


class CardCommentResponse(BaseResponse):
    """Response model returned when a single card comment is created or updated."""

    comment: DeckComment = Field(description="The created or updated card comment")


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
