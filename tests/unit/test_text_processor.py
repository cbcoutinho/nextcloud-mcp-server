"""Plain-text files are decoded, whatever their encoding."""

import pytest

from nextcloud_mcp_server.document_processors import get_registry
from nextcloud_mcp_server.document_processors.text import TextProcessor

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("content", "text", "encoding"),
    [
        ("café résumé".encode(), "café résumé", "utf-8-sig"),
        ("﻿hello".encode(), "hello", "utf-8-sig"),  # BOM stripped
        ("“quoted” – £5".encode("cp1252"), "“quoted” – £5", "cp1252"),
        (b"\x81\x8d", "\x81\x8d", "latin-1"),  # undefined in cp1252
    ],
)
async def test_decodes_utf8_then_cp1252_then_latin1(content, text, encoding):
    result = await TextProcessor().process(content, "text/plain")

    assert result.success
    assert result.text == text
    assert result.metadata["encoding"] == encoding


@pytest.mark.parametrize(
    "content_type", ["text/plain", "text/markdown", "text/csv; charset=utf-8"]
)
def test_the_registry_routes_text_types_to_it(content_type):
    processor = get_registry().find_processor(content_type)

    assert processor is not None
    assert processor.name == "text"
