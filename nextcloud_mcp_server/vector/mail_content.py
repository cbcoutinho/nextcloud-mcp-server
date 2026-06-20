"""Shared reconstruction of mail-message content for indexing and context.

The vector processor (index-time) and search context expansion (query-time)
must build the *identical* text for a mail message so chunk offsets align.
Keeping that logic here — rather than copy-pasted in both call sites — is the
single source of truth for the reconstruction.
"""

from typing import Any

from nextcloud_mcp_server.vector.html_processor import html_to_markdown


def format_mail_addresses(addrs: list[dict[str, Any]] | None) -> str:
    """Render a list of {label, email} address objects as a display string."""
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
        <blank line>
        <body>

    The body is the Mail OCS ``body`` field — sanitized HTML when
    ``hasHtmlBody`` is set (converted to Markdown for embedding), otherwise
    plain text.
    """
    subject = message.get("subject") or ""
    from_str = format_mail_addresses(message.get("from"))
    to_str = format_mail_addresses(message.get("to"))
    raw_body = message.get("body") or ""
    body_text = html_to_markdown(raw_body) if message.get("hasHtmlBody") else raw_body

    content_parts = [subject]
    if from_str:
        content_parts.append(f"From: {from_str}")
    if to_str:
        content_parts.append(f"To: {to_str}")
    content_parts.append("")  # Blank line
    content_parts.append(body_text)
    return "\n".join(content_parts)
