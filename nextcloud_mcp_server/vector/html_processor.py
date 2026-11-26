"""HTML to Markdown conversion utilities for vector sync."""

import logging
import re

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter
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
        logger.warning("Failed to convert HTML to Markdown: %s", e)
        # Fallback: strip all HTML tags as a last resort

        text = re.sub(r"<[^>]+>", " ", html_content)
        return " ".join(text.split())  # Normalize whitespace


# Elements whose *content* is never message text. ``strip=`` in markdownify
# keeps a stripped tag's inner text, which for <style>/<title> in a newsletter
# leaks CSS and the document title into the body — so these are removed whole.
_MAIL_DROP_TAGS = ("head", "title", "style", "script", "noscript", "iframe")

# A table cell holding any of these is laying out a page, not tabulating data.
_MAIL_BLOCK_TAGS = (
    "p",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "blockquote",
    "table",
    "hr",
    "pre",
)

_MAIL_TABLE_PARTS = ("table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption")

# A line made only of '-' or '=' turns the line above it into a setext heading
# in Markdown — the ``-- `` signature delimiter does this to the line before it.
_SETEXT_UNDERLINE = re.compile(r"^( {0,3})([-=]+[ \t\r]*)$", re.MULTILINE)

_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

# Mail HTML is untrusted: a cell claiming colspan="1000" would write a thousand
# pipes into every indexed row. No real data table in a mail spans more columns.
_MAX_COLSPAN = 50


def _owned_by(el: Tag, table: Tag) -> bool:
    """True when ``table`` is the nearest <table> ancestor of ``el``."""
    return el.find_parent("table") is table


def _is_layout_table(table: Tag) -> bool:
    """Decide whether a mail <table> lays out the page rather than holding data.

    HTML mail (newsletters, signatures, most marketing mail) is built from
    nested tables used purely for positioning. Converting those as Markdown
    tables squashes the whole message into one cell, so headings, lists and
    paragraphs are lost. A table counts as layout when it declares
    ``role="presentation"``, nests another table, has a cell holding block
    content, or has at most one cell per row.
    """
    if str(table.get("role") or "").lower() in ("presentation", "none"):
        return True
    rows = [tr for tr in table.find_all("tr") if _owned_by(tr, table)]
    if not rows:
        return True
    if all(len(tr.find_all(["td", "th"], recursive=False)) <= 1 for tr in rows):
        return True
    for cell in table.find_all(["td", "th"]):
        if _owned_by(cell, table) and cell.find(_MAIL_BLOCK_TAGS):
            return True
    return False


def _image_placeholder(img: Tag) -> str:
    """Text standing in for an image: ``[image: <alt>]``, or nothing without alt.

    Images are never emitted as ``![alt](src)``: the Astrolabe viewer would
    render that as a live <img>, fetching remote (tracking) images from the
    Astrolabe origin or showing a broken ``cid:`` reference. Images without alt
    text are spacers and tracking pixels, so they are dropped. Square brackets
    in the alt text are swapped for parentheses so it cannot form a link.
    """
    alt = " ".join(str(img.get("alt") or "").split())
    if not alt:
        return ""
    alt = alt.replace("[", "(").replace("]", ")")
    return f"[image: {alt}]"


class _MailMarkdownConverter(MarkdownConverter):
    """markdownify converter that keeps data tables intact."""

    def _cell(self, el: Tag, text: str) -> str:
        colspan = 1
        span = str(el.get("colspan") or "")
        if span.isdigit():
            colspan = max(1, min(_MAX_COLSPAN, int(span)))
        # A literal '|' would end the cell early and shift every column after it.
        cell = " ".join(text.split()).replace("|", r"\|")
        return " " + cell + " |" * colspan

    def convert_td(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        return self._cell(el, text)

    def convert_th(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        return self._cell(el, text)


def _prepare_mail_soup(soup: BeautifulSoup) -> None:
    """Rewrite a mail's HTML tree in place before Markdown conversion."""
    for tag in soup.find_all(_MAIL_DROP_TAGS):
        tag.decompose()

    for img in soup.find_all("img"):
        img.replace_with(_image_placeholder(img))

    # Classify every table before changing any, so a nested table's verdict is
    # not affected by its parent having already been flattened.
    layout_tables = [t for t in soup.find_all("table") if _is_layout_table(t)]
    for table in layout_tables:
        parts = [
            el
            for el in table.find_all(_MAIL_TABLE_PARTS)
            if _owned_by(el, table) and el.name != "table"
        ]
        for el in [table, *parts]:
            el.name = "div"
            el.attrs = {}


def mail_html_to_markdown(html_content: str | None) -> str:
    """Convert a mail's sanitized HTML body to Markdown the chunk viewer can render.

    Separate from :func:`html_to_markdown` because mail HTML differs from feed
    HTML. Tables are mostly page layout, images are mostly remote or ``cid:``
    references, and ``<style>``/``<head>`` blocks are common. The rules:

    - Layout tables are flattened into blocks (see :func:`_is_layout_table`).
      Data tables stay Markdown tables, with ``|`` escaped in cells and the
      first row used as the header when there is no ``<th>`` row.
    - Images become ``[image: alt]`` text, or nothing when there is no alt.
    - ``<head>``, ``<style>``, ``<script>`` and similar are dropped with their
      content.
    - Quoted replies (``<blockquote>``, ``gmail_quote``) stay ``>`` quotes and
      ``<br>`` stays a line break, as markdownify already does.
    - Runs of blank lines collapse to one.

    If the rewrite raises, the generic :func:`html_to_markdown` result is
    returned instead, so a malformed message still gets indexed.

    The output is part of the indexed text, so changing it changes chunk
    offsets. Bump ``MAIL_CONTENT_VERSION`` in ``vector/mail_content.py`` with
    any change here, so the scanner re-indexes existing mail.
    """
    if not html_content:
        return ""

    try:
        soup = BeautifulSoup(html_content, "html.parser")
        _prepare_mail_soup(soup)
        markdown = _MailMarkdownConverter(
            heading_style="ATX",
            bullets="-",
            code_language="",
            table_infer_header=True,
        ).convert_soup(soup)
    except Exception:
        logger.warning("Failed to convert mail HTML to Markdown", exc_info=True)
        return html_to_markdown(html_content)
    return _EXCESS_BLANK_LINES.sub("\n\n", markdown).strip()


def escape_setext_underlines(text: str) -> str:
    """Escape lines that Markdown would read as a setext heading underline.

    Plain-text mail is rendered as Markdown by the chunk viewer. A line of only
    ``-`` or ``=`` characters (the ``-- `` signature delimiter, or an ASCII
    underline) would turn the line above it into a heading. A leading backslash
    keeps it literal and leaves every other character in place.
    """
    return _SETEXT_UNDERLINE.sub(r"\1\\\2", text)
