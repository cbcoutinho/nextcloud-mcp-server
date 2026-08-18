"""HTTP client for the Nextcloud Talk (spreed) app.

Talk exposes its REST API under ``/ocs/v2.php/apps/spreed/api/{v}/...``.
The current versions used here are:

- conversations & participants: ``api/v4`` (Nextcloud 22+)
- chat:                          ``api/v1`` (Nextcloud 13+)

All endpoints follow the OCS envelope ``{"ocs": {"meta": ..., "data": ...}}``,
require ``OCS-APIRequest: true`` and respond as JSON when ``Accept:
application/json`` is sent.
"""

import logging
import re
from typing import Any, Literal

from nextcloud_mcp_server.client.base import BaseNextcloudClient
from nextcloud_mcp_server.models.talk import (
    TalkConversation,
    TalkMessage,
    TalkParticipant,
)

logger = logging.getLogger(__name__)


# Spreed conversation tokens are short alphanumeric strings (e.g. "a1b2c3d4").
# httpx does not normalise path traversal sequences, so a pathological token
# like ``"../foo"`` would be sent verbatim. Validate up-front for clearer
# errors and defence-in-depth.
#: The actor sources spreed accepts when adding a participant. A ``Literal``
#: rather than a bare ``str`` so an unsupported source is rejected by the schema
#: -- and shows up as an enum in the MCP tool definition -- instead of reaching
#: the server as a bare 400 that names no field.
TalkParticipantSource = Literal[
    "users", "groups", "emails", "circles", "federated_users"
]

_TALK_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+$")


def _validate_token(token: str) -> None:
    if not _TALK_TOKEN_RE.fullmatch(token):
        raise ValueError(f"Invalid Talk conversation token: {token!r}")


def _validate_message_id(message_id: int) -> None:
    """Reject message ids that cannot address a message.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, so ``True``
    would otherwise sail through as message id 1.
    """
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise ValueError(f"Message ID must be an integer: {message_id!r}")
    if message_id <= 0:
        raise ValueError(f"Message ID must be positive: {message_id}")


def _validate_reaction(reaction: str) -> None:
    """Reject an empty reaction before it reaches the wire.

    spreed rejects these too, but with a bare 400 that says nothing about which
    field was at fault.
    """
    if not reaction or not reaction.strip():
        raise ValueError("Reaction must not be empty or whitespace-only")


def _reaction_map(data: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalise spreed's reactions payload to a map.

    An empty reaction set comes back as ``{}`` on Talk 22, but PHP serialises
    empty maps as ``[]`` elsewhere in this API, and ``null`` shows up on some
    error paths. All three mean "no reactions".
    """
    if not data:
        return {}
    if not isinstance(data, dict):
        logger.warning("Unexpected reactions payload of type %s", type(data).__name__)
        return {}
    return data


class TalkClient(BaseNextcloudClient):
    """Client for Nextcloud Talk (spreed) app operations."""

    app_name = "talk"

    _ROOM_BASE = "/ocs/v2.php/apps/spreed/api/v4/room"
    _CHAT_BASE = "/ocs/v2.php/apps/spreed/api/v1/chat"
    _REACTION_BASE = "/ocs/v2.php/apps/spreed/api/v1/reaction"

    def _talk_headers(self) -> dict[str, str]:
        """Standard OCS+JSON headers for spreed API calls.

        ``Content-Type`` is intentionally omitted — httpx adds it
        automatically (and correctly) on requests that pass ``json=``,
        so setting it here would also leak it onto bodyless GETs and
        DELETEs.
        """
        return {
            "OCS-APIRequest": "true",
            "Accept": "application/json",
        }

    # Conversations (rooms)

    async def list_conversations(
        self,
        *,
        modified_since: int | None = None,
        include_status: bool = False,
        no_status_update: bool = True,
    ) -> list[TalkConversation]:
        """Return the user's Talk conversations.

        Args:
            modified_since: If provided, only return conversations modified
                after this Unix timestamp (server-side filter).
            include_status: Include user-status info for one-to-one rooms.
            no_status_update: When True (default), the call does not bump
                the user's "online" status — appropriate for an MCP server
                acting in the background.
        """
        params: dict[str, Any] = {}
        if modified_since is not None:
            params["modifiedSince"] = modified_since
        if include_status:
            params["includeStatus"] = 1
        if no_status_update:
            params["noStatusUpdate"] = 1
        response = await self._make_request(
            "GET", self._ROOM_BASE, params=params, headers=self._talk_headers()
        )
        data = response.json()["ocs"]["data"]
        return [TalkConversation(**room) for room in data]

    async def get_conversation(self, token: str) -> TalkConversation:
        """Fetch a single Talk conversation by its room token."""
        _validate_token(token)
        response = await self._make_request(
            "GET", f"{self._ROOM_BASE}/{token}", headers=self._talk_headers()
        )
        return TalkConversation(**response.json()["ocs"]["data"])

    async def create_conversation(
        self,
        *,
        room_type: int = 2,
        room_name: str,
        invite: str | None = None,
    ) -> TalkConversation:
        """Create a new conversation.

        Args:
            room_type: 1=one-to-one, 2=group, 3=public. Defaults to 2.
            room_name: Display name (required for group/public rooms).
            invite: Optional user/group ID to invite at creation time.
        """
        body: dict[str, Any] = {"roomType": room_type, "roomName": room_name}
        if invite is not None:
            body["invite"] = invite
        response = await self._make_request(
            "POST", self._ROOM_BASE, json=body, headers=self._talk_headers()
        )
        return TalkConversation(**response.json()["ocs"]["data"])

    async def delete_conversation(self, token: str) -> None:
        """Delete a conversation. Used by integration test cleanup."""
        _validate_token(token)
        await self._make_request(
            "DELETE", f"{self._ROOM_BASE}/{token}", headers=self._talk_headers()
        )

    # Chat

    async def get_messages(
        self,
        token: str,
        *,
        limit: int = 50,
        last_known_message_id: int | None = None,
        look_into_future: bool = False,
        set_read_marker: bool = False,
        include_last_known: bool = False,
    ) -> tuple[list[TalkMessage], int | None]:
        """Fetch chat messages for a conversation.

        Args:
            token: Conversation token.
            limit: Max messages to return. spreed caps this server-side
                at 200; values outside ``[1, 200]`` are clamped here so
                callers don't get a confusing mismatch between the
                requested limit and the returned ``count``.
            last_known_message_id: Pagination cursor — pass the value
                from the previous response's ``X-Chat-Last-Given`` header.
            look_into_future: When False (default), return *older*
                messages relative to ``last_known_message_id`` — i.e.,
                read history. When True, this becomes a long-poll for
                new messages, which we don't expose via MCP.
            set_read_marker: When False (default), the call does not move
                the user's read marker — consumers can call
                ``mark_as_read`` explicitly.
            include_last_known: Include the message identified by
                ``last_known_message_id`` itself in the page.

        Returns:
            ``(messages, x_chat_last_given)`` where the integer is the
            value of the ``X-Chat-Last-Given`` response header (or None
            if the header was absent or unparseable), suitable for
            pagination.
        """
        _validate_token(token)
        clamped_limit = min(max(1, limit), 200)
        params: dict[str, Any] = {
            "limit": clamped_limit,
            "lookIntoFuture": 1 if look_into_future else 0,
            "setReadMarker": 1 if set_read_marker else 0,
            "includeLastKnown": 1 if include_last_known else 0,
        }
        if last_known_message_id is not None:
            params["lastKnownMessageId"] = last_known_message_id
        response = await self._make_request(
            "GET",
            f"{self._CHAT_BASE}/{token}",
            params=params,
            headers=self._talk_headers(),
        )
        # 200 OK → JSON body with messages; 304 Not Modified → no body.
        # _make_request's raise_for_status() lets 3xx through for GET, but
        # spreed returns 200 with an empty data list when there's nothing
        # new, so we trust the JSON body here.
        last_given_header = response.headers.get("X-Chat-Last-Given")
        last_given: int | None = None
        if last_given_header:
            try:
                last_given = int(last_given_header)
            except ValueError:
                # Defensive: spreed always sends an int, but a misbehaving
                # proxy could mangle the header. Don't crash the read flow.
                logger.warning(
                    "Invalid X-Chat-Last-Given header from spreed: %r",
                    last_given_header,
                )
        data = response.json()["ocs"]["data"]
        return [TalkMessage(**msg) for msg in data], last_given

    async def send_message(
        self,
        token: str,
        message: str,
        *,
        reply_to: int | None = None,
        reference_id: str | None = None,
        silent: bool = False,
    ) -> TalkMessage:
        """Post a chat message to a conversation.

        Args:
            token: Conversation token.
            message: Message text (max 32000 chars per spreed docs/chat.md).
            reply_to: Optional parent message ID to thread this reply.
            reference_id: Optional client-provided UUID for idempotency on
                retry (spreed dedupes on this within the conversation).
            silent: When True, the message is delivered without push
                notifications.
        """
        _validate_token(token)
        body: dict[str, Any] = {"message": message}
        if reply_to is not None:
            body["replyTo"] = reply_to
        if reference_id is not None:
            body["referenceId"] = reference_id
        if silent:
            body["silent"] = True
        response = await self._make_request(
            "POST",
            f"{self._CHAT_BASE}/{token}",
            json=body,
            headers=self._talk_headers(),
        )
        return TalkMessage(**response.json()["ocs"]["data"])

    async def mark_as_read(
        self, token: str, *, last_read_message: int | None = None
    ) -> None:
        """Mark the conversation as read.

        If ``last_read_message`` is provided it sets the read marker to
        that message; otherwise spreed marks everything currently in the
        room as read.
        """
        _validate_token(token)
        body: dict[str, Any] = {}
        if last_read_message is not None:
            body["lastReadMessage"] = last_read_message
        # ``json=None`` makes httpx skip both the body and the
        # ``Content-Type: application/json`` header — semantically correct
        # for the bodyless "mark everything as read" call.
        await self._make_request(
            "POST",
            f"{self._CHAT_BASE}/{token}/read",
            json=body or None,
            headers=self._talk_headers(),
        )

    # Participants

    async def list_participants(
        self, token: str, *, include_status: bool = False
    ) -> list[TalkParticipant]:
        """List participants of a Talk conversation."""
        _validate_token(token)
        params: dict[str, Any] = {}
        if include_status:
            params["includeStatus"] = 1
        response = await self._make_request(
            "GET",
            f"{self._ROOM_BASE}/{token}/participants",
            params=params,
            headers=self._talk_headers(),
        )
        data = response.json()["ocs"]["data"]
        return [TalkParticipant(**p) for p in data]

    async def add_participant(
        self,
        token: str,
        participant: str,
        *,
        source: TalkParticipantSource = "users",
    ) -> None:
        """Add a participant to a group or public conversation.

        Returns nothing: spreed answers a successful add with an empty ``data``
        payload (verified against Talk 22.0.17), so there is no participant
        object to hand back.

        Adding someone who is already in the room succeeds as a no-op, which is
        why the tool in front of this is marked idempotent.

        Args:
            token: Conversation token.
            participant: Identifier to add, interpreted per ``source``.
            source: Actor source -- ``users`` (default), ``groups``, ``emails``,
                ``circles``, or ``federated_users``.

        Raises:
            HTTPStatusError: 404 when the participant does not exist
                (``data.error == "new-participant"``), 400 for a one-to-one
                room, which cannot take additional participants
                (``data.error == "room-type"``).
        """
        _validate_token(token)
        await self._make_request(
            "POST",
            f"{self._ROOM_BASE}/{token}/participants",
            json={"newParticipant": participant, "source": source},
            headers=self._talk_headers(),
        )

    # Reactions

    async def list_reactions(
        self, token: str, message_id: int, *, reaction: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """List reactions on a chat message, keyed by emoji.

        Args:
            token: Conversation token.
            message_id: ID of the message.
            reaction: Optional single emoji to filter to.

        Returns:
            ``{emoji: [actor, ...]}``. Empty when nothing has been reacted.
        """
        _validate_token(token)
        _validate_message_id(message_id)
        if reaction is not None:
            _validate_reaction(reaction)
        params = {"reaction": reaction} if reaction is not None else None
        response = await self._make_request(
            "GET",
            f"{self._REACTION_BASE}/{token}/{message_id}",
            params=params,
            headers=self._talk_headers(),
        )
        return _reaction_map(response.json()["ocs"]["data"])

    async def add_reaction(
        self, token: str, message_id: int, reaction: str
    ) -> dict[str, list[dict[str, Any]]]:
        """React to a chat message, returning the message's updated reactions.

        spreed answers ``201`` for a new reaction and ``200`` when the actor had
        already reacted with that emoji -- the end state is the same either way,
        which is why the tool is marked idempotent.
        """
        _validate_token(token)
        _validate_message_id(message_id)
        _validate_reaction(reaction)
        response = await self._make_request(
            "POST",
            f"{self._REACTION_BASE}/{token}/{message_id}",
            json={"reaction": reaction},
            headers=self._talk_headers(),
        )
        return _reaction_map(response.json()["ocs"]["data"])

    async def remove_reaction(
        self, token: str, message_id: int, reaction: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Remove the user's reaction, returning the updated reactions.

        Raises:
            HTTPStatusError: 404 if the user has no such reaction on the
                message. The end state matches a successful removal, but spreed
                reports it as an error rather than a no-op.
        """
        _validate_token(token)
        _validate_message_id(message_id)
        _validate_reaction(reaction)
        response = await self._make_request(
            "DELETE",
            f"{self._REACTION_BASE}/{token}/{message_id}",
            json={"reaction": reaction},
            headers=self._talk_headers(),
        )
        return _reaction_map(response.json()["ocs"]["data"])
