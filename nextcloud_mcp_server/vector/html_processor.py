"""HTML to Markdown conversion utilities for vector sync."""

import logging
import re

from markdownify import markdownify as md

logger = logging.getLogger(__name__)


def html_to_markdown(html_content: str | None) -> str:
    """Convert HTML content to Markdown, preserving semantic structure.

    This function converts HTML (typically from RSS/Atom feed items) to Markdown
    for better text embedding. Markdown preserves:
    - Heading hierarchy (important for document structure)
    - Lists (bullet and numbered)
    - Links (as [text](url))
    - Bold/italic emphasis
    - Paragraphs and line breaks

    Args:
        html_content: HTML string to convert (may be None or empty)

    Returns:
        Markdown string, or empty string if input is None/empty

    Example:
        >>> html_to_markdown("<h1>Title</h1><p>Content with <b>bold</b>.</p>")
        '# Title\\n\\nContent with **bold**.\\n\\n'
    """
    if not html_content:
        return ""

    try:
        markdown = md(
            html_content,
            heading_style="ATX",  # Use # style headings
            strip=["script", "style", "iframe", "noscript"],  # Remove unsafe elements
            bullets="-",  # Use - for unordered lists
            code_language="",  # Don't add language hints to code blocks
        )
        return markdown.strip()
    except Exception as e:
        logger.warning(f"Failed to convert HTML to Markdown: {e}")
        # Fallback: strip all HTML tags as a last resort

        text = re.sub(r"<[^>]+>", " ", html_content)
        return " ".join(text.split())  # Normalize whitespace
