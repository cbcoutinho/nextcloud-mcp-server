"""Compact JSON in tool results (GH #1395).

MCPServer serialises every non-string tool return with ``indent=2``
(``mcp.server.mcpserver.utilities.func_metadata._convert_to_content``), and this
SDK exposes no serializer hook to override it -- neither does the v2
``mcpserver`` package, so migrating does not fix it either. The consumer is a
language model with a bounded context window, and that indentation costs
16-41% of every response while carrying no information.

Re-serialising the *finished content blocks* keeps the fix on our side of the
dependency boundary. Patching the SDK's private helper would silently stop
applying if it were renamed; content blocks are protocol types that both SDK
versions return, so this cannot fail quietly.
"""

import json
from typing import Any

from mcp.types import CallToolResult, ContentBlock, TextContent


def compact_json_dumps(data: Any) -> str:
    """Serialise ``data`` as JSON with nothing spent on presentation.

    Both settings matter and are easy to half-apply, which is why they live
    together here rather than as arguments at each call site: the default
    separators ``(", ", ": ")`` spend two tokens per field on filler, and the
    default ``ensure_ascii=True`` re-escapes non-ASCII to ``\\uXXXX``, which
    costs *more* than the whitespace this saves.

    Use it for JSON a tool builds itself -- ``compact_tool_result`` cannot
    reach a single-line string a tool already returned.
    """
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def compact_json_text(text: str) -> str:
    """Strip pretty-printing from ``text`` if it is an indented JSON document.

    Only text that opens the way ``indent=2`` output does is even parsed, so a
    tool returning prose or already-compact JSON is left untouched.
    """
    if not text.startswith(("{\n", "[\n")):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    return compact_json_dumps(data)


def _compact_block(block: ContentBlock) -> ContentBlock:
    if not isinstance(block, TextContent):
        return block
    text = compact_json_text(block.text)
    return block if text is block.text else block.model_copy(update={"text": text})


def compact_tool_result(result: Any) -> Any:
    """Compact the JSON in a ``MCPServer.call_tool`` result.

    mcp 2.x returns a ``CallToolResult``; only its ``content`` is rewritten --
    ``structured_content`` is a dict the SDK serialises itself, without
    indentation. The 1.x shapes (a bare block list, or an
    ``(unstructured, structured)`` pair) are still handled because
    ``ToolManager``-level callers and tests pass them directly.
    """
    if isinstance(result, CallToolResult):
        content = [_compact_block(block) for block in result.content]
        return result.model_copy(update={"content": content})
    if isinstance(result, tuple) and len(result) == 2:
        unstructured, structured = result
        return compact_tool_result(unstructured), structured
    if isinstance(result, list):
        return [_compact_block(block) for block in result]
    return result
