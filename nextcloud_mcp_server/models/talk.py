"""Pydantic models for the Nextcloud Talk (spreed) integration."""

from typing import Any

from nextcloud_client.models.talk import (
    TalkConversation,
    TalkMessage,
    TalkParticipant,
    TalkParticipantSource,
    TalkReactionActor,
)
from pydantic import Field, field_validator

from .base import BaseResponse, StatusResponse

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


class CreateConversationResponse(BaseResponse):
    """Response model returned after creating a Talk conversation."""

    conversation: TalkConversation = Field(description="The created conversation")


class AddParticipantResponse(StatusResponse):
    """Response model returned after adding a participant.

    spreed answers a successful add with an empty ``data`` payload, so there is
    no participant object to return -- the echo of what was added is all the
    caller gets, and is what makes the result readable.
    """

    conversation_token: str = Field(description="Token of the conversation")
    participant: str = Field(description="Identifier of the added participant")
    source: TalkParticipantSource = Field(
        description="Actor source the participant was added from"
    )


class ReactionsResponse(BaseResponse):
    """Reactions on a chat message, keyed by emoji.

    spreed returns a map of emoji -> list of actors. An *empty* map arrives as
    a JSON object (``{}``) on Talk 22, but PHP serialises empty maps as ``[]``
    elsewhere in this API, so the validator below accepts either -- see the
    PHP empty-array quirk in CLAUDE.md.
    """

    conversation_token: str = Field(description="Token of the conversation")
    message_id: int = Field(description="ID of the message the reactions are on")
    reactions: dict[str, list[TalkReactionActor]] = Field(
        default_factory=dict,
        description="Emoji -> the actors who reacted with it",
    )
    distinct_emoji: int = Field(
        default=0,
        description=(
            "Number of distinct emoji on this message -- not the total number "
            "of reactions, which is the sum of the actor lists."
        ),
    )

    @field_validator("reactions", mode="before")
    @classmethod
    def _coerce_empty_list(cls, value: Any) -> Any:
        """Treat PHP's empty-list-for-empty-map as an empty map."""
        if isinstance(value, list) and not value:
            return {}
        return value
