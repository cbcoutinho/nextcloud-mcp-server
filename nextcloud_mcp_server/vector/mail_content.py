"""Shared mail-message plumbing for indexing, verification and context.

Two things live here, both because *two* call sites must agree exactly:

1. Content reconstruction (:func:`build_mail_content`) — the vector processor
   (index-time) and search context expansion (query-time) must build the
   identical text for a message so chunk offsets align.
2. The listing window (:func:`list_index_window`) — the scanner (index window)
   and the verifier (presence window) must list messages identically, or the
   verifier evicts messages the scanner legitimately indexed.
"""

from typing import Any

from nextcloud_mcp_server.client.mail import MailClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.html_processor import html_to_markdown

# Messages indexed (and verified) per mailbox. This equals the Mail API's
# per-request maximum (it clamps ``limit`` to 1..100), so it cannot be raised
# without adding cursor pagination — hence a documented constant rather than a
# config knob that would silently cap at 100. Shared by the scanner (index
# window) and the verifier (presence window) so they stay consistent.
#
# Which end of the mailbox this window covers is the *user's* Mail ``sort-order``
# preference, not ours: newest-first by default, oldest-first if they changed it.
MAIL_SCAN_MAX_PER_MAILBOX = 100


async def mail_index_filter(mail_client: MailClient) -> str | None:
    """Resolve ``MAIL_INDEX_TAG`` into a Mail listing filter for this user.

    Returns ``None`` when the setting is empty, meaning "index every message" —
    the behaviour before the setting existed. Otherwise returns ``tags:<id>``,
    where the id is *this user's* tag row: tag ids are per-user, so the filter
    has to be resolved per user rather than configured as a literal.

    Resolution goes through the create-or-get ``POST /api/tags``, which is the
    only way to look a tag up (the Mail app has no tag-listing route) and has
    the useful side effect of making the tag appear in the user's Mail tag
    picker — that picker *is* the opt-in UI for indexing.

    Deliberately not cached. One extra request per user per scan is noise next
    to the per-account/per-mailbox listing calls, and a cache would turn the one
    failure that matters — a user deleting and re-creating the tag, leaving a
    stale id that returns an empty listing rather than an error — from
    self-healing on the next tick into permanent until restart.

    Raises:
        Whatever the client raises. Callers **must not** fall back to an
        unfiltered listing: unfiltered is a *narrower* window for old tagged
        mail (the newest-N cap applies after the filter), so falling back would
        hard-evict messages that were legitimately indexed.
    """
    tag_name = get_settings().mail_index_tag
    if not tag_name:
        return None
    tag = await mail_client.ensure_tag(tag_name)
    return f"tags:{tag['id']}"


async def list_index_window(
    mail_client: MailClient,
    mailbox_id: int,
    index_filter: str | None = None,
) -> list[dict[str, Any]]:
    """List the messages of one mailbox that make up the index window.

    The single listing call shared by the scanner (which decides what to index)
    and the verifier (which decides what is still present). Pinning the limit,
    the view and the filter in one place is what keeps those two windows from
    drifting apart — a message the scanner indexes but the verifier cannot see
    is dropped from search results and evicted from the index.

    ``view="singleton"`` is load-bearing, not a default. The Mail app coerces
    anything that is not literally ``"singleton"`` to its threaded view, which
    self-joins the message table and returns only the newest message of each
    thread — so thread replies would never be indexed, and (with a filter) a
    matching message would silently drop out of the listing as soon as a reply
    arrived, because the thread self-join carries no filter predicate.

    Args:
        mail_client: The ``mail`` attribute of a Nextcloud client. Typed
            concretely so a signature change in ``MailClient.list_messages``
            fails type-checking here rather than silently drifting the window.
        mailbox_id: Mailbox database ID to list.
        index_filter: Optional Mail search-filter string, as resolved by
            :func:`mail_index_filter`; ``None`` lists every message.

    Returns:
        Message summary dicts, at most ``MAIL_SCAN_MAX_PER_MAILBOX``.
    """
    return await mail_client.list_messages(
        mailbox_id,
        limit=MAIL_SCAN_MAX_PER_MAILBOX,
        search_filter=index_filter,
        view="singleton",
    )


def format_mail_addresses(addrs: list[dict[str, Any]] | None) -> str:
    """Render a list of {label, email} address objects as a display string.

    ``None`` and address objects with neither ``label`` nor ``email`` are
    skipped (yielding ``""`` for an all-empty list) — IMAP envelope addresses
    effectively always carry at least an email, so this only drops malformed
    entries rather than losing real recipients.
    """
    parts: list[str] = []
    for addr in addrs or []:
        label = addr.get("label")
        email = addr.get("email")
        if label and email and label != email:
            parts.append(f"{label} <{email}>")
        elif email:
            parts.append(email)
        elif label:
            parts.append(label)
    return ", ".join(parts)


def build_mail_content(message: dict[str, Any]) -> str:
    """Reconstruct the indexed text body for a mail message.

    Layout (kept stable so index-time and query-time offsets match):
        <subject>
        From: <from>
        To: <to>
        Cc: <cc>          # only when non-empty
        Bcc: <bcc>        # only when non-empty
        <blank line>
        <body>

    Cc/Bcc are included so recipient-oriented queries ("emails where alice was
    cc'd") can match. The body is the Mail OCS ``body`` field — sanitized HTML
    when ``hasHtmlBody`` is set (converted to Markdown for embedding), otherwise
    plain text.
    """
    subject = message.get("subject") or ""
    from_str = format_mail_addresses(message.get("from"))
    to_str = format_mail_addresses(message.get("to"))
    cc_str = format_mail_addresses(message.get("cc"))
    bcc_str = format_mail_addresses(message.get("bcc"))
    raw_body = message.get("body") or ""
    body_text = html_to_markdown(raw_body) if message.get("hasHtmlBody") else raw_body

    content_parts = [subject]
    if from_str:
        content_parts.append(f"From: {from_str}")
    if to_str:
        content_parts.append(f"To: {to_str}")
    if cc_str:
        content_parts.append(f"Cc: {cc_str}")
    if bcc_str:
        content_parts.append(f"Bcc: {bcc_str}")
    content_parts.append("")  # Blank line
    content_parts.append(body_text)
    return "\n".join(content_parts)
