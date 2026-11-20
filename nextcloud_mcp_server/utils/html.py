"""HTML to Markdown conversion, shared across layers.

Lives in ``utils`` rather than ``vector`` because ``document_processors`` needs
it too, and that package must not import ``vector`` (see
``vector/spool.py``'s module docstring, and the ``TYPE_CHECKING``-guarded lazy
imports in ``vector/processor.py`` for issue #877). Importing
``vector.html_processor`` from a processor would run ``vector/__init__.py``,
pulling ``qdrant-client`` and ``langchain-text-splitters`` onto the document
stack's import path -- the same heavy cross-layer coupling #877 removed, in the
opposite direction. This module depends only on ``markdownify``.
"""

import logging
import re

from markdownify import markdownify as md

logger = logging.getLogger(__name__)


def html_to_markdown(html_content: str | None) -> str:
    """Convert HTML content to Markdown, preserving semantic structure.

    Preserves heading hierarchy, lists, links, emphasis, paragraphs and tables
    -- the structure that makes an embedded chunk searchable rather than a wall
    of words.

    Args:
        html_content: HTML string to convert (may be None or empty)

    Returns:
        Markdown string, or empty string if input is None/empty

    Example:
        >>> html_to_markdown("<h1>Title</h1><p>Content with <b>bold</b>.</p>")
        '# Title\\n\\nContent with **bold**.'
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
        logger.warning("Failed to convert HTML to Markdown: %s", e)
        # Fallback: strip all HTML tags as a last resort. Note this returns
        # flattened prose, NOT markdown -- a caller that reports a parse mode
        # must not assume a non-empty result means structure was preserved.

        text = re.sub(r"<[^>]+>", " ", html_content)
        return " ".join(text.split())  # Normalize whitespace
