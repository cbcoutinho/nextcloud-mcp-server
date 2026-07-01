"""MCP tools for Nextcloud Mail app (read, send)."""

import json
import logging

from httpx import HTTPStatusError, RequestError
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.mail import (
    GetAttachmentResponse,
    GetMessageResponse,
    ListAccountsResponse,
    ListMailboxesResponse,
    ListMessagesResponse,
    MailAccount,
    MailMailbox,
    MailMessage,
    MailMessageSummary,
    SendMessageResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)

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


def configure_mail_tools(mcp: FastMCP):
    """Configure Mail app MCP tools (read, send)."""

    @mcp.tool(
        title="List Mail Accounts",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
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
            raise McpError(
                ErrorData(code=-1, message=f"Network error listing accounts: {str(e)}")
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to list accounts: {e.response.status_code}",
                )
            )

    @mcp.tool(
        title="List Mail Mailboxes",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
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
            raise McpError(
                ErrorData(code=-1, message=f"Network error listing mailboxes: {str(e)}")
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to list mailboxes: {e.response.status_code}",
                )
            )

    @mcp.tool(
        title="List Mail Messages",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
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

        Reads cached envelope metadata (fast); does not fetch bodies. Use
        nc_mail_get_message to fetch a full body.

        Args:
            mailbox_id: Numeric mailbox id (``database_id`` from nc_mail_list_mailboxes)
            cursor: Pagination cursor from a prior page
            search_filter: Optional search/filter query
            limit: Max messages to return (1-100, default 20)

        Returns:
            ListMessagesResponse with message summaries. ``has_more`` is a
            heuristic (true when exactly ``limit`` messages were returned), so it
            can be a false positive when a mailbox holds exactly ``limit``
            messages; page with ``cursor`` and stop on an empty result.
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
            raise McpError(
                ErrorData(code=-1, message=f"Network error listing messages: {str(e)}")
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to list messages: {e.response.status_code}",
                )
            )

    @mcp.tool(
        title="Get Mail Message",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
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
                raise McpError(
                    ErrorData(code=-1, message=f"Message {message_id} not found")
                )
            message = MailMessage(**message_data)
            return GetMessageResponse(message=message)
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Network error getting message {message_id}: {str(e)}",
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(
                    ErrorData(code=-1, message=f"Message {message_id} not found")
                )
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to get message {message_id}: "
                    f"{e.response.status_code}",
                )
            )

    @mcp.tool(
        title="Send Mail Message",
        annotations=ToolAnnotations(
            idempotentHint=False,  # Stages a new outbox entry each call (ADR-017)
            openWorldHint=True,
        ),
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
            return SendMessageResponse(
                success=True, message="Message sent successfully"
            )
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error sending message: {str(e)}")
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to send message: {e.response.status_code} "
                    f"{e.response.text[:500]}",
                )
            )
        except json.JSONDecodeError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Invalid JSON in recipient list: {str(e)}")
            )

    @mcp.tool(
        title="Get Mail Attachment",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
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
            is the attachment body base64-encoded; large attachments produce a
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
            raise McpError(
                ErrorData(
                    code=-1, message=f"Network error getting attachment: {str(e)}"
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(ErrorData(code=-1, message="Attachment not found"))
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to get attachment: {e.response.status_code}",
                )
            )
