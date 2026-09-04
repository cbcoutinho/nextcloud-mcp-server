"""MCP tool registration for the Nextcloud Talk (spreed) integration."""

import logging
import uuid

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.capabilities import require_capability
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.talk import (
    AddParticipantResponse,
    CreateConversationResponse,
    GetConversationResponse,
    ListConversationsResponse,
    ListMessagesResponse,
    ListParticipantsResponse,
    MarkAsReadResponse,
    ReactionsResponse,
    SendMessageResponse,
    TalkParticipantSource,
    TalkRoomType,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


# spreed advertises a 32000-character limit on chat messages (docs/chat.md);
# we enforce it client-side for a clearer error than the server's 413.
_MESSAGE_MAX_LENGTH = 32000


def _validate_message_text(message: str) -> None:
    # Reject both empty strings and whitespace-only strings — spreed
    # would happily post the latter as a visually-blank message.
    if not message or not message.strip():
        raise ToolError("Message text must not be empty or whitespace-only")
    if len(message) > _MESSAGE_MAX_LENGTH:
        raise ToolError(
            f"Message too long: {len(message)} characters (max {_MESSAGE_MAX_LENGTH})"
        )


def configure_talk_tools(mcp: MCPServer) -> None:
    """Configure Nextcloud Talk (spreed) MCP tools."""

    # Read tools

    @mcp.tool(
        title="List Talk Conversations",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("talk.read")
    @instrument_tool
    async def talk_list_conversations(
        ctx: Context,
        modified_since: int | None = None,
        include_status: bool = False,
    ) -> ListConversationsResponse:
        """List the user's Talk conversations (rooms).

        Args:
            modified_since: Optional Unix timestamp. Only conversations
                modified after this time are returned.
            include_status: Whether to include user-status info for
                one-to-one conversations.
        """
        client = await get_client(ctx)
        rooms = await client.talk.list_conversations(
            modified_since=modified_since,
            include_status=include_status,
        )
        return ListConversationsResponse(results=rooms, total=len(rooms))

    @mcp.tool(
        title="Get Talk Conversation",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("talk.read")
    @instrument_tool
    async def talk_get_conversation(
        ctx: Context, token: str
    ) -> GetConversationResponse:
        """Get details of a Talk conversation by its token.

        Args:
            token: Unique room token (returned by ``talk_list_conversations``).
        """
        client = await get_client(ctx)
        conversation = await client.talk.get_conversation(token)
        return GetConversationResponse(conversation=conversation)

    @mcp.tool(
        title="Get Talk Messages",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("talk.read")
    @instrument_tool
    async def talk_get_messages(
        ctx: Context,
        token: str,
        limit: int = 50,
        last_known_message_id: int | None = None,
        include_last_known: bool = False,
    ) -> ListMessagesResponse:
        """Read chat history for a Talk conversation.

        Returns the most recent messages (older first when paginated).
        Does not move the user's read marker. Call
        ``talk_mark_as_read`` separately if desired.

        Args:
            token: Conversation token.
            limit: Max messages per page. Valid range is 1-200 (spreed
                caps server-side at 200). Values outside this range are
                clamped. Default 50.
            last_known_message_id: Pagination cursor — pass the
                ``last_known_message_id`` from the previous response to
                fetch the next (older) page.
            include_last_known: Include the cursor message in the page
                instead of starting just before it.
        """
        client = await get_client(ctx)
        messages, last_given = await client.talk.get_messages(
            token,
            limit=limit,
            last_known_message_id=last_known_message_id,
            look_into_future=False,
            set_read_marker=False,
            include_last_known=include_last_known,
        )
        return ListMessagesResponse(
            conversation_token=token,
            results=messages,
            count=len(messages),
            last_known_message_id=last_given,
        )

    @mcp.tool(
        title="List Talk Conversation Participants",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("talk.read")
    @instrument_tool
    async def talk_list_participants(
        ctx: Context, token: str, include_status: bool = False
    ) -> ListParticipantsResponse:
        """List the participants of a Talk conversation.

        Args:
            token: Conversation token.
            include_status: Include each participant's user-status info.
        """
        client = await get_client(ctx)
        participants = await client.talk.list_participants(
            token, include_status=include_status
        )
        return ListParticipantsResponse(
            conversation_token=token,
            results=participants,
            count=len(participants),
        )

    # Write tools

    @mcp.tool(
        title="Send Talk Message",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("talk.write")
    @instrument_tool
    async def talk_send_message(
        ctx: Context,
        token: str,
        message: str,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> SendMessageResponse:
        """Post a chat message into a Talk conversation as the user.

        A random ``referenceId`` is attached so spreed dedupes the post
        if the request is retried.

        Args:
            token: Conversation token.
            message: Message text (max 32000 characters).
            reply_to: Optional parent message ID to thread the reply.
            silent: When True the message is delivered without push
                notifications (e.g. for status updates).
        """
        _validate_message_text(message)
        client = await get_client(ctx)
        posted = await client.talk.send_message(
            token,
            message,
            reply_to=reply_to,
            # 32 hex chars, no dashes — spreed accepts either UUID format.
            reference_id=uuid.uuid4().hex,
            silent=silent,
        )
        return SendMessageResponse(message=posted)

    @mcp.tool(
        title="Mark Talk Conversation as Read",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("talk.write")
    @instrument_tool
    async def talk_mark_as_read(
        ctx: Context,
        token: str,
        last_read_message: int | None = None,
    ) -> MarkAsReadResponse:
        """Move the user's read marker forward in a Talk conversation.

        Args:
            token: Conversation token.
            last_read_message: Optional message ID to mark as the new
                read position. When omitted, spreed marks everything
                currently in the room as read.
        """
        client = await get_client(ctx)
        await client.talk.mark_as_read(token, last_read_message=last_read_message)
        return MarkAsReadResponse(
            success=True,
            message="Conversation marked as read",
            conversation_token=token,
            last_read_message=last_read_message,
        )

    @mcp.tool(
        title="Create Talk Conversation",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("talk.write")
    @instrument_tool
    async def talk_create_conversation(
        ctx: Context,
        room_name: str | None = None,
        room_type: TalkRoomType = 2,
        invite: str | None = None,
    ) -> CreateConversationResponse:
        """Create a new Talk conversation (room).

        Args:
            room_name: Display name for the room. Required for a group (2) or
                public (3) room. Omit it for a one-to-one room, which spreed
                names after the other participant.
            room_type: 2 for a group room (default), 3 for a public room, 1 for
                a one-to-one room.
            invite: User or group ID to add at creation time. Required for a
                one-to-one room, where it is the other participant and is what
                defines the room. Optional otherwise.
        """
        if room_type == 1:
            # A one-to-one room is defined by who is in it, not by a name --
            # spreed accepts the create with no roomName at all. Without an
            # invite there is no second participant, so there is no room.
            if not invite or not invite.strip():
                raise ToolError(
                    "invite is required for a one-to-one room (room_type=1): "
                    "the other participant is what identifies the room"
                )
        elif not room_name or not room_name.strip():
            raise ToolError(
                f"room_name is required for room_type={room_type} and must not "
                "be empty or whitespace-only"
            )

        client = await get_client(ctx)
        conversation = await client.talk.create_conversation(
            room_type=room_type,
            room_name=room_name,
            invite=invite,
        )
        return CreateConversationResponse(conversation=conversation)

    @mcp.tool(
        title="Add Talk Participant",
        annotations=ToolAnnotations(
            # Adding someone already in the room succeeds as a no-op (verified
            # against Talk 22.0.17), so repeating the call leaves the same state.
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    @require_scopes("talk.write")
    @instrument_tool
    async def talk_add_participant(
        ctx: Context,
        token: str,
        participant: str,
        source: TalkParticipantSource = "users",
    ) -> AddParticipantResponse:
        """Add a participant to a group or public Talk conversation.

        A one-to-one conversation cannot take additional participants -- the
        server rejects that with a room-type error.

        Args:
            token: Conversation token.
            participant: Identifier to add, interpreted according to `source`.
            source: Where the identifier comes from - "users" (default),
                "groups", "emails", "circles", or "federated_users".
        """
        if not participant or not participant.strip():
            raise ToolError("participant must not be empty or whitespace-only")

        client = await get_client(ctx)
        await client.talk.add_participant(token, participant, source=source)
        return AddParticipantResponse(
            conversation_token=token,
            participant=participant,
            source=source,
            message=f"Added {participant} ({source}) to the conversation",
        )

    @mcp.tool(
        title="List Talk Message Reactions",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("talk.read")
    @require_capability("spreed", feature="reactions")
    @instrument_tool
    async def talk_list_reactions(
        ctx: Context,
        token: str,
        message_id: int,
        reaction: str | None = None,
    ) -> ReactionsResponse:
        """List the reactions on a Talk chat message, grouped by emoji.

        Args:
            token: Conversation token.
            message_id: ID of the message to read reactions from.
            reaction: Optional single emoji to filter to.
        """
        client = await get_client(ctx)
        reactions = await client.talk.list_reactions(
            token, message_id, reaction=reaction
        )
        return ReactionsResponse(
            conversation_token=token,
            message_id=message_id,
            reactions=reactions,
            distinct_emoji=len(reactions),
        )

    @mcp.tool(
        title="React to Talk Message",
        annotations=ToolAnnotations(
            # Reactions are a set: reacting twice with the same emoji leaves the
            # same state (spreed answers 201 then 200).
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    @require_scopes("talk.write")
    @require_capability("spreed", feature="reactions")
    @instrument_tool
    async def talk_react(
        ctx: Context,
        token: str,
        message_id: int,
        reaction: str,
    ) -> ReactionsResponse:
        """React to a Talk chat message with an emoji.

        Returns the message's reactions after the change, so the caller does
        not need a follow-up read.

        Args:
            token: Conversation token.
            message_id: ID of the message to react to.
            reaction: The emoji to react with.
        """
        client = await get_client(ctx)
        reactions = await client.talk.add_reaction(token, message_id, reaction)
        return ReactionsResponse(
            conversation_token=token,
            message_id=message_id,
            reactions=reactions,
            distinct_emoji=len(reactions),
        )

    @mcp.tool(
        title="Remove Talk Message Reaction",
        annotations=ToolAnnotations(
            destructive_hint=True,
            # Same end state when repeated, though the server reports the second
            # removal as a 404 rather than a no-op.
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    @require_scopes("talk.write")
    @require_capability("spreed", feature="reactions")
    @instrument_tool
    async def talk_remove_reaction(
        ctx: Context,
        token: str,
        message_id: int,
        reaction: str,
    ) -> ReactionsResponse:
        """Remove the user's own reaction from a Talk chat message.

        Removing a reaction the user has not made is reported by the server as
        a not-found error rather than succeeding silently.

        Args:
            token: Conversation token.
            message_id: ID of the message to remove the reaction from.
            reaction: The emoji to remove.
        """
        client = await get_client(ctx)
        reactions = await client.talk.remove_reaction(token, message_id, reaction)
        return ReactionsResponse(
            conversation_token=token,
            message_id=message_id,
            reactions=reactions,
            distinct_emoji=len(reactions),
        )
