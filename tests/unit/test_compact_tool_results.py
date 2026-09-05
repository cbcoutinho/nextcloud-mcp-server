"""Unit tests for compact tool-result serialisation (GH #1395)."""

import json

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.models.base import BaseResponse
from nextcloud_mcp_server.serialization import (
    compact_json_dumps,
    compact_json_text,
    compact_tool_result,
)

pytestmark = pytest.mark.unit


def _text(result) -> str:
    """The unstructured text of a call_tool result, whichever shape it has."""
    if isinstance(result, CallToolResult):
        blocks = result.content
    elif isinstance(result, tuple):
        blocks = result[0]
    else:
        blocks = result
    return blocks[0].text


def test_indented_json_object_is_compacted():
    assert (
        compact_json_text('{\n  "a": 1,\n  "b": [\n    2\n  ]\n}') == '{"a":1,"b":[2]}'
    )


def test_non_ascii_survives_unescaped():
    # \uXXXX escaping would cost more tokens than the indentation removed.
    assert compact_json_text('{\n  "n": "Björn"\n}') == '{"n":"Björn"}'


def test_compact_dumps_spends_nothing_on_presentation():
    """The half-applied version of this cost a review round: separators without
    ensure_ascii=False still pays the \\uXXXX tax on any non-ASCII field."""
    text = compact_json_dumps({"owner": "Björn", "path": "/a b", "n": 1})

    assert text == '{"owner":"Björn","path":"/a b","n":1}'
    assert "\\u" not in text
    assert ", " not in text


def test_prose_and_compact_json_are_left_alone():
    for text in ("Share 5 deleted", '{"a": 1}', "[not json\n]", '{\n  "a": ,}'):
        assert compact_json_text(text) is text


def test_non_text_blocks_pass_through_untouched():
    image = ImageContent(type="image", data="Zm9v", mimeType="image/png")
    assert compact_tool_result([image]) == [image]


def test_unrecognised_result_shapes_pass_through():
    """A shape neither SDK returns today must survive an SDK change untouched.

    A tuple that is not the ``(unstructured, structured)`` pair is the shape
    most likely to appear if the SDK grows a third element, so it is worth
    naming alongside the arbitrary object.
    """
    sentinel = {"some": "future shape"}
    assert compact_tool_result(sentinel) is sentinel

    triple = ([TextContent(type="text", text='{\n  "a": 1\n}')], {"a": 1}, "extra")
    assert compact_tool_result(triple) is triple


def test_structured_half_of_the_pair_is_preserved():
    unstructured = [TextContent(type="text", text='{\n  "a": 1\n}')]
    structured = {"a": 1}

    compacted, passed_through = compact_tool_result((unstructured, structured))

    assert compacted[0].text == '{"a":1}'
    assert passed_through is structured


async def test_call_tool_returns_unindented_json():
    """The end-to-end path: MCPServer's own indent=2 must not reach the client."""

    class Item(BaseResponse):
        name: str
        tags: list[str]

    mcp = NextcloudMCPServer("test")

    @mcp.tool()
    async def get_item() -> Item:
        return Item(name="thing", tags=["a", "b"])

    text = _text(await mcp.call_tool("get_item", {}))

    assert "\n" not in text
    assert json.loads(text)["tags"] == ["a", "b"]
