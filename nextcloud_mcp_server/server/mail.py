"""MCP tools for Nextcloud Mail app (read, send, flags, tags, move, delete)."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from httpx import HTTPStatusError, RequestError
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations
from pydantic import Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client.mail import DEFAULT_TAG_COLOR
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.mail import (
    GetAttachmentResponse,
    GetMessageResponse,
    GetMessageSourceResponse,
    ListAccountsResponse,
    ListMailboxesResponse,
    ListMessagesResponse,
    MailAccount,
    MailActionResponse,
    MailMailbox,
    MailMessage,
    MailMessageSummary,
    MailTag,
    MailTagResponse,
    SendMessageResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


@contextmanager
def _mail_errors(action: str) -> Iterator[None]:
    """Map client transport errors onto ``MCPError`` with a consistent message.

    Args:
        action: Present-participle description used in the message, e.g.
            ``"setting flags on message 42"``.
    """
    try:
        yield
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error {action}: {str(e)}") from e
    except HTTPStatusError as e:
        raise MCPError(
            code=-1, message=f"Failed {action}: HTTP {e.response.status_code}"
        ) from e


# Hard cap on inlined attachment content. The Mail attachment endpoint returns
# the full attachment body (base64-encoded) in the response, which then has to
# fit in the host LLM's context window; replace anything larger with a sentinel
# so a 20 MB design file can't blow up the MCP response. Callers can still see
# the real size via the message's attachment list.
#
# NOTE: the cap is deliberately measured against the *base64-encoded* string —
# that encoded footprint (~1.33x the raw file) is exactly what lands in the MCP
# response, which is the thing we're bounding. Sizing off the raw byte count
# would let a correspondingly larger payload through into the response.
MAX_ATTACHMENT_CONTENT_BYTES = 5 * 1024 * 1024


def _cap_attachment_content(content: str | None) -> str | None:
    """Replace oversized attachment content with a size sentinel.

    Measures the UTF-8 byte length of the (base64-encoded) content — i.e. the
    footprint that actually lands in the MCP response / LLM context — not the
    raw file size or character count. Non-string content is returned unchanged.
    """
    if not isinstance(content, str):
        return content
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > MAX_ATTACHMENT_CONTENT_BYTES:
        return (
            f"[attachment too large to inline: {content_bytes} bytes "
            f"(> {MAX_ATTACHMENT_CONTENT_BYTES})]"
        )
    return content


@require_scopes("mail.read")
@instrument_tool
async def nc_mail_list_accounts(ctx: Context) -> ListAccountsResponse:
    """List the user's configured mail accounts (requires mail.read scope)."""
    client = await get_client(ctx)
    try:
        accounts_data = await client.mail.list_accounts()
        accounts = [MailAccount(**a) for a in accounts_data]
        return ListAccountsResponse(results=accounts, total_count=len(accounts))
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error listing accounts: {str(e)}")
    except HTTPStatusError as e:
        raise MCPError(
            code=-1,
            message=f"Failed to list accounts: {e.response.status_code}",
        )


@require_scopes("mail.read")
@instrument_tool
async def nc_mail_list_mailboxes(
    account_id: int, ctx: Context
) -> ListMailboxesResponse:
    """List the mailboxes (folders) of a mail account (requires mail.read scope).

    Args:
        account_id: Account ID (from nc_mail_list_accounts)

    Returns:
        ListMailboxesResponse with mailboxes. Use a mailbox's ``database_id``
        with nc_mail_list_messages.
    """
    client = await get_client(ctx)
    try:
        mailboxes_data = await client.mail.get_mailboxes(account_id)
        mailboxes = [MailMailbox(**m) for m in mailboxes_data]
        return ListMailboxesResponse(results=mailboxes, total_count=len(mailboxes))
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error listing mailboxes: {str(e)}")
    except HTTPStatusError as e:
        raise MCPError(
            code=-1,
            message=f"Failed to list mailboxes: {e.response.status_code}",
        )


@require_scopes("mail.read")
@instrument_tool
async def nc_mail_list_messages(
    mailbox_id: int,
    ctx: Context,
    cursor: int | None = None,
    search_filter: str | None = None,
    limit: int = 20,
) -> ListMessagesResponse:
    """List message envelopes in a mailbox, newest first (requires mail.read scope).

    Reads cached envelope metadata (fast). Does not fetch bodies. Use
    nc_mail_get_message to fetch a full body.

    Args:
        mailbox_id: Numeric mailbox id (``database_id`` from nc_mail_list_mailboxes)
        cursor: Pagination cursor from a prior page
        search_filter: Optional filter query — space-separated ``token:value``
            terms, ANDed together. Supported tokens:

            * ``is:``/``not:`` — ``read``, ``unread``, ``starred``,
              ``answered``, ``important``
            * ``from:``, ``to:``, ``cc:``, ``bcc:`` — substring match on the
              address or display name
            * ``subject:``, ``body:`` — substring match (``body:`` searches
              IMAP server-side and is slower)
            * ``tags:`` — comma-separated tag *database ids* (not names,
              get one from nc_mail_create_tag)
            * ``start:``, ``end:`` — date bounds
            * ``flags:`` — comma-separated ``read``/``unread``/``starred``/
              ``answered``/``important``/``attachments``
            * ``match:anyof`` — OR the from/to/cc/bcc/subject/body terms
              instead of ANDing them

            Example: ``is:unread from:alice subject:invoice``
        limit: Max messages to return (1-100, default 20)

    Returns:
        ListMessagesResponse with message summaries. ``has_more`` is a
        heuristic (true when exactly ``limit`` messages were returned), so it
        can be a false positive when a mailbox holds exactly ``limit``
        messages. Page with ``cursor`` and stop on an empty result.
    """
    client = await get_client(ctx)
    # Clamp to the same window the client/OCS API enforce so the has_more
    # heuristic compares against the limit actually applied (a caller passing
    # limit<=0 otherwise gets a misleading count).
    effective_limit = min(max(1, limit), 100)
    try:
        messages_data = await client.mail.list_messages(
            mailbox_id,
            cursor=cursor,
            search_filter=search_filter,
            limit=effective_limit,
        )
        messages = [MailMessageSummary(**m) for m in messages_data]
        return ListMessagesResponse(
            results=messages,
            total_count=len(messages),
            has_more=len(messages) == effective_limit,
        )
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error listing messages: {str(e)}")
    except HTTPStatusError as e:
        raise MCPError(
            code=-1,
            message=f"Failed to list messages: {e.response.status_code}",
        )


@require_scopes("mail.read")
@instrument_tool
async def nc_mail_get_message(message_id: int, ctx: Context) -> GetMessageResponse:
    """Get a single mail message with its full body (requires mail.read scope).

    The Mail app fetches the body from IMAP server-side.

    Args:
        message_id: Numeric message id (``database_id`` from nc_mail_list_messages)

    Returns:
        GetMessageResponse with the full message including body and attachments.
        Attachments with ``id: null`` are inline body parts and cannot be
        fetched via nc_mail_get_attachment (which requires a string id).
    """
    client = await get_client(ctx)
    try:
        message_data = await client.mail.get_message(message_id)
        # An empty payload (OCS data=null with a 200 meta) would make
        # MailMessage(**{}) raise an uncaught ValidationError; treat it as
        # not-found instead.
        if not message_data:
            raise MCPError(code=-1, message=f"Message {message_id} not found")
        message = MailMessage(**message_data)
        return GetMessageResponse(message=message)
    except RequestError as e:
        raise MCPError(
            code=-1,
            message=f"Network error getting message {message_id}: {str(e)}",
        )
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise MCPError(code=-1, message=f"Message {message_id} not found")
        raise MCPError(
            code=-1,
            message=f"Failed to get message {message_id}: {e.response.status_code}",
        )


@require_scopes("mail.send")
@instrument_tool
async def nc_mail_send_message(
    account_id: int,
    to: str,
    subject: str,
    body: str,
    ctx: Context,
    is_html: bool = False,
    cc: str | None = None,
    bcc: str | None = None,
    references: str | None = None,
) -> SendMessageResponse:
    """Send an email through a configured Nextcloud Mail account (requires mail.send scope).

    The ``From:`` identity is derived by the Mail app from ``account_id``.
    Recipients are specified as JSON arrays of ``{"label": "...", "email": "..."}``
    objects.  Example for ``to``::

        [{"label": "John Doe", "email": "john@example.com"}]

    Args:
        account_id: Mail account ID to send from (from nc_mail_list_accounts)
        to: JSON array of To recipients
        subject: Email subject
        body: Email body (plain text unless is_html is true)
        is_html: Whether body contains HTML (default false)
        cc: Optional JSON array of CC recipients
        bcc: Optional JSON array of BCC recipients
        references: Optional RFC 2822 Message-ID for reply threading

    Returns:
        SendMessageResponse with success status and optional message
    """
    client = await get_client(ctx)
    try:
        to_list = json.loads(to)  # `to` is a required JSON-array string
        cc_list = json.loads(cc) if isinstance(cc, str) else (cc or [])
        bcc_list = json.loads(bcc) if isinstance(bcc, str) else (bcc or [])

        await client.mail.send_message(
            account_id=account_id,
            to=to_list,
            subject=subject,
            body=body,
            is_html=is_html,
            cc=cc_list or None,
            bcc=bcc_list or None,
            references=references or None,
        )
        return SendMessageResponse(success=True, message="Message sent successfully")
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error sending message: {str(e)}")
    except HTTPStatusError as e:
        raise MCPError(
            code=-1,
            message=f"Failed to send message: {e.response.status_code} "
            f"{e.response.text[:500]}",
        )
    except json.JSONDecodeError as e:
        raise MCPError(code=-1, message=f"Invalid JSON in recipient list: {str(e)}")


@require_scopes("mail.read")
@instrument_tool
async def nc_mail_get_attachment(
    message_id: int, attachment_id: str, ctx: Context
) -> GetAttachmentResponse:
    """Get a single mail attachment's metadata and content (requires mail.read scope).

    Args:
        message_id: Numeric message id
        attachment_id: Attachment id (a string, from the message's attachments)

    Returns:
        GetAttachmentResponse with name, mime, size, and content. ``content``
        is the attachment body base64-encoded. Large attachments produce a
        correspondingly large response, so prefer the ``size`` from the
        message's attachment list before fetching.
    """
    client = await get_client(ctx)
    try:
        data = await client.mail.get_attachment(message_id, attachment_id)
        return GetAttachmentResponse(
            name=data.get("name"),
            mime=data.get("mime"),
            size=data.get("size"),
            content=_cap_attachment_content(data.get("content")),
        )
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error getting attachment: {str(e)}")
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise MCPError(code=-1, message="Attachment not found")
        raise MCPError(
            code=-1,
            message=f"Failed to get attachment: {e.response.status_code}",
        )


@require_scopes("mail.read")
@instrument_tool
async def nc_mail_get_message_source(
    message_id: int, ctx: Context
) -> GetMessageSourceResponse:
    """Get a message's raw RFC 2822 source (requires mail.read scope).

    The complete original message including all headers — use this when the
    parsed view from nc_mail_get_message is not enough (checking
    Received/DKIM headers, List-Unsubscribe, custom X- headers).

    Args:
        message_id: Numeric message id

    Returns:
        GetMessageSourceResponse with the raw source. ``source`` is None if
        the Mail app returns no content for the message.
    """
    client = await get_client(ctx)
    with _mail_errors(f"getting source of message {message_id}"):
        source = await client.mail.get_message_raw(message_id)
    return GetMessageSourceResponse(message_id=message_id, source=source)


@require_scopes("mail.write")
@instrument_tool
async def nc_mail_set_flags(
    message_id: int,
    ctx: Context,
    seen: bool | None = None,
    flagged: bool | None = None,
    answered: bool | None = None,
    junk: bool | None = None,
) -> MailActionResponse:
    """Set IMAP flags on a message, e.g. mark it read (requires mail.write scope).

    Only the flags you pass are changed. Omitted ones are left alone. Use
    ``seen=True`` after processing a message so it does not look unhandled,
    and ``seen=False`` to mark it unread again.

    Args:
        message_id: Numeric message id (``database_id`` from nc_mail_list_messages)
        seen: Mark read (True) or unread (False)
        flagged: Star (True) or unstar (False)
        answered: Mark as replied-to
        junk: Mark as junk/spam. Unlike the others this is a custom IMAP
            keyword, so the mail server silently ignores it unless the
            mailbox permits custom keywords.

    Returns:
        MailActionResponse naming the flags that were applied. Passing no
        flags at all is an error rather than a silent no-op.
    """
    flags = {
        name: value
        for name, value in (
            ("seen", seen),
            ("flagged", flagged),
            ("answered", answered),
            ("$junk", junk),
        )
        if value is not None
    }
    if not flags:
        raise MCPError(
            code=-1,
            message="No flags given; pass at least one of seen, flagged, "
            "answered, junk",
        )

    client = await get_client(ctx)
    with _mail_errors(f"setting flags on message {message_id}"):
        await client.mail.set_flags(message_id, flags)

    applied = ", ".join(f"{name}={value}" for name, value in flags.items())
    return MailActionResponse(
        message_id=message_id, message=f"Flags updated: {applied}"
    )


@require_scopes("mail.write")
@instrument_tool
async def nc_mail_create_tag(
    display_name: Annotated[str, Field(max_length=128)],
    ctx: Context,
    color: str = DEFAULT_TAG_COLOR,
) -> MailTagResponse:
    """Create a mail tag, or return the existing one (requires mail.write scope).

    Tags are IMAP keywords private to the user. The Mail app has no
    tag-listing endpoint, so this is also how you look a tag up — calling it
    for an existing name returns that tag rather than creating a duplicate.

    Names normalise to a lowercase IMAP label with spaces as underscores, so
    "AI Index", "ai index" and "ai_index" are all the same tag.

    Args:
        display_name: Tag name (max 128 characters)
        color: Hex colour, applied only when the tag is created

    Returns:
        MailTagResponse with the tag, including the ``id`` needed for the
        ``tags:<id>`` filter of nc_mail_list_messages.
    """
    client = await get_client(ctx)
    with _mail_errors(f"creating tag {display_name!r}"):
        tag = await client.mail.ensure_tag(display_name, color)
    return MailTagResponse(tag=MailTag(**tag))


@require_scopes("mail.write")
@instrument_tool
async def nc_mail_set_tag(
    message_id: int, tag: Annotated[str, Field(max_length=128)], ctx: Context
) -> MailTagResponse:
    """Assign a tag to a message (requires mail.write scope).

    The tag is created if it does not exist yet, so this works without a
    separate nc_mail_create_tag call.

    Args:
        message_id: Numeric message id
        tag: Tag display name

    Returns:
        MailTagResponse with the assigned tag.
    """
    client = await get_client(ctx)
    with _mail_errors(f"tagging message {message_id} with {tag!r}"):
        # Resolve through create-or-get: it yields the server's own
        # imapLabel, so we never have to re-derive the label encoding.
        tag_data = await client.mail.ensure_tag(tag)
        await client.mail.set_tag(message_id, tag_data["imapLabel"])
    return MailTagResponse(tag=MailTag(**tag_data), message_id=message_id)


@require_scopes("mail.write")
@instrument_tool
async def nc_mail_remove_tag(
    message_id: int, tag: Annotated[str, Field(max_length=128)], ctx: Context
) -> MailTagResponse:
    """Remove a tag from a message (requires mail.write scope).

    Args:
        message_id: Numeric message id
        tag: Tag display name

    Returns:
        MailTagResponse with the removed tag.
    """
    client = await get_client(ctx)
    with _mail_errors(f"removing tag {tag!r} from message {message_id}"):
        # Resolve the same way as tagging. For a name that has never been
        # used this creates an empty tag row before removing nothing from
        # the message — harmless, and it keeps one resolution path.
        tag_data = await client.mail.ensure_tag(tag)
        await client.mail.remove_tag(message_id, tag_data["imapLabel"])
    return MailTagResponse(tag=MailTag(**tag_data), message_id=message_id)


@require_scopes("mail.write")
@instrument_tool
async def nc_mail_move_message(
    message_id: int, destination_mailbox_id: int, ctx: Context
) -> MailActionResponse:
    """Move a message to another mailbox (requires mail.write scope).

    Args:
        message_id: Numeric message id
        destination_mailbox_id: Target mailbox id (``database_id`` from
            nc_mail_list_mailboxes)

    Returns:
        MailActionResponse. ``message_id`` echoes what you passed in and is
        **stale on return**: the move invalidates it, and the message is
        re-cached in the destination under a new id. Re-list the destination
        mailbox to address it again — do not retry this call with the same
        id, which fails rather than repeating the move.
    """
    client = await get_client(ctx)
    with _mail_errors(f"moving message {message_id}"):
        await client.mail.move_message(message_id, destination_mailbox_id)
    return MailActionResponse(
        message_id=message_id,
        message=f"Message moved to mailbox {destination_mailbox_id}",
    )


@require_scopes("mail.write")
@instrument_tool
async def nc_mail_delete_message(message_id: int, ctx: Context) -> MailActionResponse:
    """Delete a message (requires mail.write scope).

    The Mail app moves the message to the account's trash mailbox, or
    expunges it permanently if it is already in trash. Requires the account
    to have a trash mailbox — without one the Mail app rejects the call
    ("No trash mailbox configured").

    Args:
        message_id: Numeric message id

    Returns:
        MailActionResponse confirming the deletion. ``message_id`` echoes
        the input and is **stale on return** — trashing re-caches the
        message under a new id, so retrying with the same id fails.
    """
    client = await get_client(ctx)
    with _mail_errors(f"deleting message {message_id}"):
        await client.mail.delete_message(message_id)
    return MailActionResponse(message_id=message_id, message="Message deleted")


def configure_mail_tools(mcp: MCPServer):
    """Configure Mail app MCP tools (read, send, flags, tags, move, delete)."""

    mcp.tool(
        title="List Mail Accounts",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_mail_list_accounts)

    mcp.tool(
        title="List Mail Mailboxes",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_mail_list_mailboxes)

    mcp.tool(
        title="List Mail Messages",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_mail_list_messages)

    mcp.tool(
        title="Get Mail Message",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_mail_get_message)

    mcp.tool(
        title="Send Mail Message",
        annotations=ToolAnnotations(
            idempotent_hint=False,  # Stages a new outbox entry each call (ADR-017)
            open_world_hint=True,
        ),
    )(nc_mail_send_message)

    mcp.tool(
        title="Get Mail Attachment",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_mail_get_attachment)

    mcp.tool(
        title="Get Mail Message Source",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_mail_get_message_source)

    mcp.tool(
        title="Set Mail Message Flags",
        annotations=ToolAnnotations(
            # Same inputs -> same end state (a flag set twice stays set).
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )(nc_mail_set_flags)

    mcp.tool(
        title="Create Mail Tag",
        annotations=ToolAnnotations(
            # Create-or-get: the Mail app returns the existing tag for a name
            # that already resolves to the same IMAP label.
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )(nc_mail_create_tag)

    mcp.tool(
        title="Tag Mail Message",
        annotations=ToolAnnotations(
            idempotent_hint=True,  # Re-tagging a tagged message is a no-op
            open_world_hint=True,
        ),
    )(nc_mail_set_tag)

    mcp.tool(
        title="Untag Mail Message",
        annotations=ToolAnnotations(
            idempotent_hint=True,  # Removing an absent tag leaves the same state
            open_world_hint=True,
        ),
    )(nc_mail_remove_tag)

    mcp.tool(
        title="Move Mail Message",
        annotations=ToolAnnotations(
            # NOT idempotent. The move drops the cached row and the message is
            # re-cached in the destination under a *new* database id, so a retry
            # with the same message_id is rejected (the Mail app answers 403)
            # rather than being a harmless no-op. Verified against a live Mail
            # app -- see test_move_message_between_mailboxes.
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )(nc_mail_move_message)

    mcp.tool(
        title="Delete Mail Message",
        annotations=ToolAnnotations(
            destructive_hint=True,  # Removes the message from its mailbox
            # NOT idempotent, unlike most deletes. Trashing is a move, so it
            # invalidates the id: a retry is rejected (403) instead of being a
            # no-op, and if the message was already in trash the first call
            # expunges it permanently. Verified against a live Mail app.
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )(nc_mail_delete_message)
