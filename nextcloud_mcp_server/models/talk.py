"""Pydantic models for the Nextcloud Talk (spreed) integration."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .base import BaseResponse, StatusResponse

# Domain models


class TalkMessage(BaseModel):
    """A single chat message in a Talk conversation.

    See spreed docs/chat.md for the field definitions. We map only the
    fields that are useful to MCP consumers; spreed returns more.
    """

    id: int
    token: str
    actorType: str
    actorId: str
    actorDisplayName: str
    timestamp: int
    systemMessage: str = ""
    messageType: str
    message: str
    messageParameters: dict[str, Any] = Field(default_factory=dict)
    expirationTimestamp: int | None = None
    referenceId: str | None = None
    markdown: bool | None = None

    @field_validator("messageParameters", mode="before")
    @classmethod
    def _coerce_empty_list_params(cls, v: Any) -> Any:
        # spreed serializes an empty parameter map as `[]` (PHP array) rather
        # than `{}`; normalize so pydantic accepts it as a dict.
        if isinstance(v, list) and not v:
            return {}
        return v


class TalkConversation(BaseModel):
    """A Talk conversation (room).

    See spreed docs/conversation.md for the full field reference. Many
    optional fields are omitted; we keep the ones useful for chat-centric
    flows.
    """

    # ``populate_by_name=True`` lets us deserialize spreed's ``type`` key
    # into the ``room_type`` field while still allowing internal callers
    # to construct the model with ``room_type=...`` directly.
    model_config = ConfigDict(populate_by_name=True)

    id: int
    token: str
    # The spreed JSON wire format uses ``type`` for the room kind, but
    # ``type`` shadows Python's builtin within the class scope, which
    # would silently call this int field if anyone wrote ``type(...)``
    # in a validator or method on this model. Map to ``room_type`` and
    # alias the wire field instead.
    room_type: int = Field(
        alias="type",
        description=(
            "Conversation kind: 1=one-to-one, 2=group, 3=public, "
            "4=changelog, 5=former one-to-one, 6=note-to-self."
        ),
    )
    name: str
    displayName: str
    description: str = ""
    participantType: int | None = None
    unreadMessages: int = 0
    unreadMention: bool = False
    lastActivity: int | None = None
    # spreed returns `0` (not `null`) when the user has not read any
    # message in the room yet — compare to ``None`` explicitly rather
    # than relying on truthiness, since `0` is falsy but valid.
    lastReadMessage: int | None = None
    lastMessage: TalkMessage | None = None
    readOnly: int | None = None
    isFavorite: bool | None = None
    notificationLevel: int | None = None
    objectType: str | None = None
    objectId: str | None = None

    @field_validator("lastMessage", mode="before")
    @classmethod
    def _coerce_empty_last_message(cls, v: Any) -> Any:
        # spreed returns `lastMessage: []` (PHP empty array) when there has
        # never been a message in the room; normalize to None.
        if isinstance(v, list) and not v:
            return None
        return v


class TalkParticipant(BaseModel):
    """A participant (attendee) in a Talk conversation."""

    attendeeId: int
    actorType: str
    actorId: str
    displayName: str
    participantType: int
    inCall: int = 0
    lastPing: int = 0
    sessionIds: list[str] = Field(default_factory=list)
    status: str | None = None
    statusIcon: str | None = None
    statusMessage: str | None = None


# Response wrappers for MCP tools


class ListConversationsResponse(BaseResponse):
    """Response model for listing Talk conversations."""

    results: list[TalkConversation] = Field(
        description="Talk conversations the user participates in"
    )
    total: int = Field(description="Number of conversations returned")


class GetConversationResponse(BaseResponse):
    """Response model for fetching a single Talk conversation."""

    conversation: TalkConversation = Field(description="The Talk conversation")


class CreateConversationResponse(BaseResponse):
    """Response model returned after creating a Talk conversation."""

    conversation: TalkConversation = Field(
        description="The newly created Talk conversation (includes token)"
    )


class ListMessagesResponse(BaseResponse):
    """Response model for fetching chat history of a conversation."""

    conversation_token: str = Field(description="Token of the conversation")
    results: list[TalkMessage] = Field(description="Chat messages in this page")
    count: int = Field(description="Number of messages returned in this page")
    last_known_message_id: int | None = Field(
        default=None,
        description=(
            "ID to pass back as `last_known_message_id` to fetch the next "
            "page (older history). Sourced from the `X-Chat-Last-Given` "
            "response header."
        ),
    )


class ListParticipantsResponse(BaseResponse):
    """Response model for listing participants in a Talk conversation."""

    conversation_token: str = Field(description="Token of the conversation")
    results: list[TalkParticipant] = Field(
        description="Participants of the conversation"
    )
    count: int = Field(description="Number of participants returned")


class SendMessageResponse(BaseResponse):
    """Response model returned after posting a chat message."""

    message: TalkMessage = Field(description="The posted chat message")


class MarkAsReadResponse(StatusResponse):
    """Response model for the mark-as-read operation."""

    conversation_token: str = Field(description="Token of the conversation")
    last_read_message: int | None = Field(
        default=None,
        description="The message ID that was marked as the last-read marker",
    )
