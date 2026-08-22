"""HTML to Markdown conversion utilities for vector sync.

The implementation moved to :mod:`nextcloud_mcp_server.utils.html` so that
``document_processors`` can use it without importing ``vector`` — see that
module's docstring. This re-export keeps the vector-side call sites (RSS/Atom
feed items, mail bodies, chunk context) importing from where they always have.
"""

from nextcloud_mcp_server.utils.html import html_to_markdown

__all__ = ["html_to_markdown"]
