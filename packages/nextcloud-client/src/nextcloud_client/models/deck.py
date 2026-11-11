from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


class DeckUser(BaseModel):
    primaryKey: str
    uid: str
    displayname: str


class DeckPermissions(BaseModel):
    PERMISSION_READ: bool
    PERMISSION_EDIT: bool
    PERMISSION_MANAGE: bool
    PERMISSION_SHARE: bool


#: Shared wording for the deep links, so every Deck response describes them the
#: same way. Populated by ``links.with_links``; see ADR-035. Deck has no route
#: that opens a single stack, so a stack links to its board.
BOARD_URL_DESCRIPTION = (
    "Link that opens this board in the Nextcloud Deck app. None when the server "
    "has no browser-reachable Nextcloud base URL configured."
)
CARD_URL_DESCRIPTION = (
    "Link that opens this card in the Nextcloud Deck app. Offer it to the user "
    "when referring to the card so they can open it in place. None when the "
    "server has no browser-reachable Nextcloud base URL configured, or when the "
    "card's board is not known from the response or the tool's arguments."
)


ATTACHMENT_URL_DESCRIPTION = (
    "Link that opens this attachment. For type='file' attachments that is the "
    "shared file in Nextcloud's Files UI; for type='deck_file' it is Deck's own "
    "download route. None when the server has no browser-reachable Nextcloud "
    "base URL configured."
)


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
    url: str | None = Field(default=None, description=BOARD_URL_DESCRIPTION)
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
    startdate: Optional[datetime] = None  # Deck >= 1.18
    done: Optional[datetime] = None
    color: Optional[str] = None  # Deck >= 1.18; bare hex, e.g. "FF0000"
    lastModified: Optional[int] = None
    createdAt: Optional[int] = None
    labels: Optional[List[DeckLabel]] = None
    assignedUsers: Optional[List[Union[DeckUser, DeckAssignedUser]]] = None
    dependentCards: Optional[List[int]] = None  # IDs of cards this card depends on
    attachments: Optional[List["DeckAttachment"]] = None
    attachmentCount: Optional[int] = None
    deletedAt: Optional[int] = None
    commentsUnread: Optional[int] = None
    commentsCount: Optional[int] = None
    lastEditor: Optional[str] = None
    overdue: Optional[int] = None
    etag: Optional[str] = Field(default=None, alias="ETag")
    url: str | None = Field(default=None, description=CARD_URL_DESCRIPTION)

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


class DeckCardSummary(BaseModel):
    """Compact projection of a :class:`DeckCard` for list/overview views.

    Drops the heavy fields that dominate token cost when many cards are
    returned at once — the full ``description`` (kept only as a short
    ``descriptionPreview``), the nested ``labels``/``assignedUsers``/
    ``attachments`` objects (kept as flat title/uid lists + counts), and the
    ``etag``/``order``/``*Modified``/``createdAt``/``deletedAt``/``overdue``/
    ``type``/``owner`` bookkeeping fields. Fetch a single card with
    ``deck_get_card`` (or ``detail="full"``) when the full body is needed.
    """

    id: int
    title: str
    stackId: int
    archived: bool = False
    duedate: datetime | None = None
    startdate: datetime | None = None
    done: datetime | None = None
    color: str | None = Field(default=None, description="Card color (bare hex)")
    labels: list[str] = Field(
        default_factory=list, description="Label titles assigned to the card"
    )
    assignedUsers: list[str] = Field(
        default_factory=list, description="UIDs of users assigned to the card"
    )
    attachmentCount: int | None = Field(
        default=None, description="Number of attachments on the card"
    )
    commentsUnread: int | None = Field(
        default=None, description="Number of unread comments"
    )
    commentsCount: int | None = Field(
        default=None, description="Total number of comments"
    )
    hasDescription: bool = Field(
        default=False, description="Whether the card has a non-empty description"
    )
    descriptionPreview: str | None = Field(
        default=None, description="Truncated preview of the card description"
    )
    url: str | None = Field(default=None, description=CARD_URL_DESCRIPTION)


class DeckStack(BaseModel):
    id: int
    title: str
    boardId: int
    order: int
    deletedAt: int
    lastModified: Optional[int] = None
    # Cards may be projected to DeckCardSummary when a tool is called with
    # detail="summary" (the default for list tools).
    cards: list[DeckCard | DeckCardSummary] | None = None
    etag: Optional[str] = Field(default=None, alias="ETag")
    url: str | None = Field(
        default=None,
        description=(
            "Link that opens this stack's board in the Nextcloud Deck app; Deck "
            "has no route that opens a single stack. None when the server has no "
            "browser-reachable Nextcloud base URL configured."
        ),
    )


class DeckAttachmentExtendedData(BaseModel):
    # Every field is optional: Deck leaves extendedData empty when the
    # underlying file is gone or unreadable (FileService/FilesAppService
    # extendData bail out), which happens routinely for deleted attachments.
    filesize: int | None = None
    mimetype: str | None = None
    info: Dict[str, str] = Field(default_factory=dict)
    # Populated for type="file" (Files share) attachments via FilesAppService.
    path: str | None = None
    fileid: int | None = None
    hasPreview: bool | None = None
    permissions: int | None = None

    @field_validator("info", mode="before")
    @classmethod
    def validate_info(cls, v):
        # PHP serialises an empty pathinfo() as [], not {}.
        return v or {}


class DeckAttachment(BaseModel):
    id: int
    cardId: int
    type: str
    data: str
    lastModified: int
    createdAt: int
    createdBy: str
    deletedAt: int
    extendedData: DeckAttachmentExtendedData = Field(
        default_factory=DeckAttachmentExtendedData
    )
    url: str | None = Field(default=None, description=ATTACHMENT_URL_DESCRIPTION)

    @field_validator("extendedData", mode="before")
    @classmethod
    def validate_extended_data(cls, v):
        return v or {}


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


class DeckCommentSummary(BaseModel):
    """Compact projection of a :class:`DeckComment` for list views.

    Drops ``mentions``, ``actorType``, ``actorDisplayName`` and ``replyTo``,
    keeping only the fields needed to read the conversation. Long messages are
    truncated by the tool layer via ``message_max_length``.
    """

    id: int
    actorId: str
    message: str
    creationDateTime: datetime


class DeckSession(BaseModel):
    token: str


class DeckConfig(BaseModel):
    calendar: bool
    cardDetailsInModal: bool
    cardIdBadge: bool
    groupLimit: Optional[List[Dict[str, str]]] = None


# Response Models for MCP Tools
