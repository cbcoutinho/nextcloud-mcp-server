"""Pydantic models for Nextcloud Mail app responses."""

from pydantic import BaseModel, ConfigDict, Field

from .base import BaseResponse, StatusResponse


class MailAddress(BaseModel):
    """An email address with an optional display label."""

    model_config = ConfigDict(populate_by_name=True)

    label: str | None = Field(None, description="Display name")
    email: str | None = Field(None, description="Email address")


class MailAccount(BaseModel):
    """A configured mail account (from the ``/api/accounts`` endpoint)."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(description="Account ID")
    # Mail 5.x's /api/accounts returns the address as ``emailAddress`` (not
    # ``email``); ``populate_by_name`` keeps the field accessible as ``email``.
    email: str = Field(alias="emailAddress", description="Account email address")
    is_delegated: bool = Field(
        False, alias="isDelegated", description="Whether this is a delegated account"
    )


class MailMailbox(BaseModel):
    """A mailbox (folder) within an account."""

    model_config = ConfigDict(populate_by_name=True)

    # ``databaseId`` is the numeric id used by list_messages; ``id`` is a
    # base64-encoded mailbox name (a string), so we expose the numeric one.
    database_id: int = Field(alias="databaseId", description="Numeric mailbox ID")
    name: str = Field(description="IMAP mailbox name (e.g. INBOX, Sent)")
    display_name: str | None = Field(
        None, alias="displayName", description="Human-readable mailbox name"
    )
    account_id: int = Field(alias="accountId", description="Parent account ID")
    special_use: list[str] = Field(
        default_factory=list,
        alias="specialUse",
        description="Special-use roles (e.g. inbox, sent, trash)",
    )
    unread: int = Field(0, description="Number of unread messages")


class MailMessageFlags(BaseModel):
    """IMAP flags on a message."""

    model_config = ConfigDict(populate_by_name=True)

    seen: bool = False
    flagged: bool = False
    answered: bool = False
    deleted: bool = False
    draft: bool = False
    forwarded: bool = False
    has_attachments: bool = Field(False, alias="hasAttachments")
    important: bool = False


class MailTag(BaseModel):
    """A mail tag (an IMAP keyword, scoped to one user).

    ``imap_label`` is derived by the server from the display name and is what
    the tag assign/remove routes address, so it is carried through rather than
    re-derived client-side.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(description="Tag database ID (per-user)")
    display_name: str | None = Field(None, alias="displayName")
    imap_label: str | None = Field(
        None, alias="imapLabel", description="IMAP keyword, e.g. $vector_index"
    )
    color: str | None = Field(None, description="Hex colour")


class MailAttachment(BaseModel):
    """Metadata for a message attachment."""

    model_config = ConfigDict(populate_by_name=True)

    # Attachment id is a string and may be null for inline/body-part messages.
    id: str | None = Field(None, description="Attachment ID")
    file_name: str | None = Field(
        None, alias="fileName", description="Attachment file name"
    )
    mime: str | None = Field(None, description="MIME type")
    size: int | None = Field(None, description="Size in bytes")
    cid: str | None = Field(None, description="Content-ID (for inline attachments)")
    disposition: str | None = Field(
        None, description="Content disposition (attachment/inline)"
    )


class MailMessageSummary(BaseModel):
    """Lightweight message envelope for mailbox listings."""

    model_config = ConfigDict(populate_by_name=True)

    # ``databaseId`` is the numeric id passed to get_message.
    database_id: int = Field(alias="databaseId", description="Numeric message ID")
    uid: int | None = Field(None, description="IMAP UID")
    subject: str | None = Field(None, description="Message subject")
    date_int: int | None = Field(
        None, alias="dateInt", description="Sent date as a Unix timestamp (seconds)"
    )
    from_: list[MailAddress] = Field(
        default_factory=list, alias="from", description="Sender addresses"
    )
    to: list[MailAddress] = Field(
        default_factory=list, description="Recipient addresses"
    )
    mailbox_id: int | None = Field(
        None, alias="mailboxId", description="Parent mailbox ID"
    )
    preview_text: str | None = Field(
        None, alias="previewText", description="Short preview snippet"
    )
    flags: MailMessageFlags | None = Field(None, description="IMAP flags")


class MailMessage(BaseModel):
    """Full message with body (from the message/{id} endpoint)."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(description="Numeric message ID")
    uid: int | None = Field(None, description="IMAP UID")
    message_id: str | None = Field(
        None, alias="messageId", description="RFC Message-ID header"
    )
    subject: str | None = Field(None, description="Message subject")
    date_int: int | None = Field(
        None, alias="dateInt", description="Sent date as a Unix timestamp (seconds)"
    )
    from_: list[MailAddress] = Field(
        default_factory=list, alias="from", description="Sender addresses"
    )
    to: list[MailAddress] = Field(
        default_factory=list, description="Recipient addresses"
    )
    cc: list[MailAddress] = Field(default_factory=list, description="CC addresses")
    bcc: list[MailAddress] = Field(default_factory=list, description="BCC addresses")
    has_html_body: bool = Field(
        False, alias="hasHtmlBody", description="Whether the body is HTML"
    )
    body: str | None = Field(
        None,
        description="Rendered body (sanitized HTML if hasHtmlBody, else plain text)",
    )
    attachments: list[MailAttachment] = Field(
        default_factory=list, description="Message attachments"
    )
    # The message endpoint returns flags just like the listing does. Carrying
    # them here is what makes nc_mail_set_flags verifiable: without it the only
    # way to read back a flag you just wrote is to re-list the whole mailbox.
    flags: MailMessageFlags | None = Field(None, description="IMAP flags")


# --- Response Models ---


class ListAccountsResponse(BaseResponse):
    """Response model for listing mail accounts."""

    results: list[MailAccount] = Field(description="List of mail accounts")
    total_count: int = Field(description="Total number of accounts")


class ListMailboxesResponse(BaseResponse):
    """Response model for listing mailboxes."""

    results: list[MailMailbox] = Field(description="List of mailboxes")
    total_count: int = Field(description="Total number of mailboxes")


class ListMessagesResponse(BaseResponse):
    """Response model for listing message envelopes."""

    results: list[MailMessageSummary] = Field(description="List of message summaries")
    total_count: int = Field(
        description="Number of messages returned in this page (NOT the mailbox "
        "total, which isn't known without a full scan); page with cursor and "
        "stop on an empty result"
    )
    has_more: bool = Field(False, description="Whether more messages may exist")


class GetMessageResponse(BaseResponse):
    """Response model for getting a single full message."""

    message: MailMessage = Field(description="Full message details")


class SendMessageResponse(BaseResponse):
    """Response model for sending a message."""

    success: bool = Field(
        default=True, description="Whether the message was sent successfully"
    )
    message: str = Field(default="", description="Status or error message")


class MailActionResponse(StatusResponse):
    """Response model for a write that returns no data (flags, move, delete).

    The Mail app answers these with an empty body, so the useful payload is the
    affected message ID plus the human-readable summary from ``StatusResponse``.
    """

    message_id: int = Field(description="Database ID of the affected message")


class MailTagResponse(BaseResponse):
    """Response model for tag create/assign/remove."""

    tag: MailTag = Field(description="The affected tag")
    message_id: int | None = Field(
        None, description="Message the tag was assigned to or removed from"
    )


class GetMessageSourceResponse(BaseResponse):
    """Response model for the raw RFC 2822 source of a message."""

    message_id: int = Field(description="Database ID of the message")
    source: str | None = Field(None, description="Raw RFC 2822 message source")


class GetAttachmentResponse(BaseResponse):
    """Response model for getting a single attachment.

    Intentionally does NOT nest ``MailAttachment``: the get-attachment path
    returns a different shape (``name``/``mime``/``size``/``content``) than the
    attachment entries on a message listing, which ``MailAttachment`` models
    (``id``/``fileName``/``cid``/``disposition``, and no ``content``).
    """

    name: str | None = Field(None, description="Attachment file name")
    mime: str | None = Field(None, description="MIME type")
    size: int | None = Field(None, description="Size in bytes")
    content: str | None = Field(None, description="Attachment content, base64-encoded")
