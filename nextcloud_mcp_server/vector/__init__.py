"""Vector database and background sync package.

`processor` and `scanner` are intentionally NOT re-exported from this
package init: they transitively import `server.semantic` ->
`search.bm25_hybrid`, which forms an import cycle with
`search.algorithms` -> `vector.placeholder` -> `vector/__init__`.
Consumers that need those symbols import them from their submodules
directly (e.g. `from nextcloud_mcp_server.vector.processor import ...`).

Nothing is re-exported at all: importing any submodule runs this file, and
some submodules (``spool``, ``payload_keys``) are used outside the optional
semantic stack, so the package itself must not import qdrant-client.
"""
