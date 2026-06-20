"""Shared reconstruction of mail-message content for indexing and context.

The vector processor (index-time) and search context expansion (query-time)
must build the *identical* text for a mail message so chunk offsets align.
Keeping that logic here — rather than copy-pasted in both call sites — is the
single source of truth for the reconstruction.
"""

from typing import Any

from nextcloud_mcp_server.vector.html_processor import html_to_markdown

# Newest-N messages indexed (and verified) per mailbox. This equals the Mail
# OCS API's per-request maximum (it clamps ``limit`` to 1..100), so it cannot be
# raised without adding cursor pagination — hence a documented constant rather
# than a config knob that would silently cap at 100. Shared by the scanner
# (index window) and the verifier (presence window) so they stay consistent.
MAIL_SCAN_MAX_PER_MAILBOX = 100


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
