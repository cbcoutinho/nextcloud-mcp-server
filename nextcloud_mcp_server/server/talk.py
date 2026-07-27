"""MCP tool registration for the Nextcloud Talk (spreed) integration."""

import logging
import uuid

from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.talk import (
    AddParticipantResponse,
    CreateConversationResponse,
    GetConversationResponse,
    ListConversationsResponse,
    ListMessagesResponse,
    ListParticipantsResponse,
    ListReactionsResponse,
    MarkAsReadResponse,
    ReactResponse,
    SendMessageResponse,
    TalkReactionActor,
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
        raise ValueError("Message text must not be empty or whitespace-only")
    if len(message) > _MESSAGE_MAX_LENGTH:
        raise ValueError(
            f"Message too long: {len(message)} characters (max {_MESSAGE_MAX_LENGTH})"
        )


def configure_talk_tools(mcp: FastMCP) -> None:
    """Configure Nextcloud Talk (spreed) MCP tools."""

    # Read tools

    @mcp.tool(
        title="List Talk Conversations",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
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
            modified_since: Optional Unix timestamp; only conversations
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
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
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
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
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
        Does not move the user's read marker; call
        ``talk_mark_as_read`` separately if desired.

        Args:
            token: Conversation token.
            limit: Max messages per page. Valid range is 1-200 (spreed
                caps server-side at 200); values outside this range are
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
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
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
        title="Create Talk Conversation",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("talk.write")
    @instrument_tool
    async def talk_create_conversation(
        ctx: Context,
        room_type: int = 2,
        room_name: str = "",
        invite: str | None = None,
    ) -> CreateConversationResponse:
        """Create a new Talk conversation (one-to-one, group, or public).

        - room_type=1: private DM — requires ``invite`` (other user id).
        - room_type=2: private group — requires ``room_name``; optional
          ``invite`` (one user) is added after create. For more people call
          ``talk_add_participant`` repeatedly.
        - room_type=3: public room — requires ``room_name`` (open link /
          reports channel). Add members with ``talk_add_participant`` if needed.

        Returns ``token`` for ``talk_send_message``. Never reuse a type=1
        DM token as a “shared” room — others will get 404.

        Args:
            room_type: 1=one-to-one, 2=group, 3=public. Defaults to 2.
            room_name: Display name (required for group/public).
            invite: User id. Required for one-to-one. Optional single
                first member for group/public (added after create).
        """
        if room_type == 1 and not (invite or "").strip():
            raise McpError(
                ErrorData(
                    code=-32602,
                    message=(
                        "invite (other user id) is required for "
                        "one-to-one conversations"
                    ),
                )
            )
        if room_type in (2, 3) and not (room_name or "").strip():
            raise McpError(
                ErrorData(
                    code=-32602,
                    message="room_name is required for group/public conversations",
                )
            )
        client = await get_client(ctx)
        # Group/public: create without invite (spreed often 404s invite-on-create
        # for type 2), then add the first member explicitly.
        create_invite = invite if room_type == 1 else None
        conversation = await client.talk.create_conversation(
            room_type=room_type,
            room_name=room_name or (invite or ""),
            invite=create_invite,
        )
        if room_type in (2, 3) and (invite or "").strip():
            await client.talk.add_participant(
                conversation.token, user_id=invite.strip()
            )
        return CreateConversationResponse(conversation=conversation)

    @mcp.tool(
        title="Add Talk Participant",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("talk.write")
    @instrument_tool
    async def talk_add_participant(
        ctx: Context,
        token: str,
        user_id: str,
        source: str = "users",
    ) -> AddParticipantResponse:
        """Invite a user into an existing Talk group/public conversation.

        Use after ``talk_create_conversation`` (room_type 2 or 3) to bring
        in a second, third, … colleague. Does not work meaningfully on
        one-to-one rooms (type 1) — create a group instead.

        Args:
            token: Conversation token from create/list.
            user_id: Nextcloud login to invite (e.g. alice).
            source: Usually ``users``.
        """
        if not (user_id or "").strip():
            raise McpError(
                ErrorData(code=-32602, message="user_id must not be empty")
            )
        client = await get_client(ctx)
        await client.talk.add_participant(
            token, user_id=user_id.strip(), source=source or "users"
        )
        return AddParticipantResponse(
            success=True,
            message="Participant invited",
            conversation_token=token,
            user_id=user_id.strip(),
            source=source or "users",
        )

    @mcp.tool(
        title="Send Talk Message",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
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
        annotations=ToolAnnotations(idempotentHint=True, openWorldHint=True),
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

    def _reactions_map(
        raw: dict,
    ) -> dict[str, list[TalkReactionActor]]:
        out: dict[str, list[TalkReactionActor]] = {}
        for emoji, actors in (raw or {}).items():
            out[str(emoji)] = [
                TalkReactionActor(**a) for a in actors if isinstance(a, dict)
            ]
        return out

    @mcp.tool(
        title="List Talk Reactions",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("talk.read")
    @instrument_tool
    async def talk_list_reactions(
        ctx: Context,
        token: str,
        message_id: int,
        reaction: str | None = None,
    ) -> ListReactionsResponse:
        """Who reacted to a Talk message (emoji → actors).

        Args:
            token: Conversation token.
            message_id: Chat message id.
            reaction: Optional single emoji to filter.
        """
        client = await get_client(ctx)
        raw = await client.talk.list_reactions(
            token, int(message_id), reaction=reaction
        )
        return ListReactionsResponse(
            conversation_token=token,
            message_id=int(message_id),
            results=_reactions_map(raw),
        )

    @mcp.tool(
        title="React to Talk Message",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("talk.write")
    @instrument_tool
    async def talk_react(
        ctx: Context,
        token: str,
        message_id: int,
        reaction: str,
    ) -> ReactResponse:
        """Add an emoji reaction to a Talk message (👍, ❤️, …).

        Args:
            token: Conversation token.
            message_id: Target message id.
            reaction: Single emoji string.
        """
        client = await get_client(ctx)
        raw = await client.talk.add_reaction(token, int(message_id), reaction)
        return ReactResponse(
            conversation_token=token,
            message_id=int(message_id),
            reaction=(reaction or "").strip(),
            results=_reactions_map(raw),
        )

    @mcp.tool(
        title="Remove Talk Reaction",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("talk.write")
    @instrument_tool
    async def talk_delete_reaction(
        ctx: Context,
        token: str,
        message_id: int,
        reaction: str,
    ) -> ReactResponse:
        """Remove your emoji reaction from a Talk message.

        Args:
            token: Conversation token.
            message_id: Target message id.
            reaction: Emoji to remove.
        """
        client = await get_client(ctx)
        raw = await client.talk.delete_reaction(token, int(message_id), reaction)
        return ReactResponse(
            conversation_token=token,
            message_id=int(message_id),
            reaction=(reaction or "").strip(),
            results=_reactions_map(raw),
        )
