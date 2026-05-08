"""Vector database and background sync package.

`processor` and `scanner` are intentionally NOT re-exported from this
package init: they transitively import `server.semantic` ->
`search.bm25_hybrid`, which forms an import cycle with
`search.algorithms` -> `vector.placeholder` -> `vector/__init__`.
Consumers that need those symbols import them from their submodules
directly (e.g. `from nextcloud_mcp_server.vector.processor import ...`).
"""

from .document_chunker import DocumentChunker
from .qdrant_client import get_qdrant_client

__all__ = [
    "get_qdrant_client",
    "DocumentChunker",
]
