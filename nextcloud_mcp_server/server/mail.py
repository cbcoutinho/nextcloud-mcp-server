"""MCP tools for Nextcloud Mail app (read-only)."""

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
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


def configure_mail_tools(mcp: FastMCP):
    """Configure Mail app MCP tools (read-only)."""

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
        try:
            messages_data = await client.mail.list_messages(
                mailbox_id, cursor=cursor, search_filter=search_filter, limit=limit
            )
            messages = [MailMessageSummary(**m) for m in messages_data]
            return ListMessagesResponse(
                results=messages,
                total_count=len(messages),
                has_more=len(messages) == limit and limit > 0,
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
        """
        client = await get_client(ctx)
        try:
            message_data = await client.mail.get_message(message_id)
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
            is the attachment body as returned by the Mail OCS API; large
            attachments produce a correspondingly large response, so prefer the
            ``size`` from the message's attachment list before fetching.
        """
        client = await get_client(ctx)
        try:
            data = await client.mail.get_attachment(message_id, attachment_id)
            return GetAttachmentResponse(
                name=data.get("name"),
                mime=data.get("mime"),
                size=data.get("size"),
                content=data.get("content"),
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
