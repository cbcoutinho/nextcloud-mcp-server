"""Pydantic models for the Nextcloud Talk (spreed) integration."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The actor sources spreed accepts when adding a participant. A ``Literal``
#: rather than a bare ``str`` so an unsupported source is rejected by the schema
#: -- and appears as an enum in the MCP tool definition -- instead of reaching
#: the server as a bare 400 that names no field.
TalkParticipantSource = Literal[
    "users", "groups", "emails", "circles", "federated_users"
]

#: The room types spreed accepts: 1=one-to-one, 2=group, 3=public. A ``Literal``
#: for the same reason as the source above.
TalkRoomType = Literal[1, 2, 3]

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
    # Present when the room's spreed "threads" feature is enabled (OCS
    # capability spreed.features includes "threads"); undocumented in
    # docs/chat.md at the time of writing. threadId is the id of the
    # thread's root message; isThread is True on that root message itself.
    threadId: int | None = None
    isThread: bool | None = None

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


class TalkReactionActor(BaseModel):
    """One actor's reaction to a chat message."""

    actorType: str
    actorId: str
    actorDisplayName: str = ""
    timestamp: int = 0
