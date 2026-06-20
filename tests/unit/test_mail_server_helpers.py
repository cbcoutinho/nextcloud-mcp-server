"""Unit tests for server/mail.py helper logic."""

import pytest

from nextcloud_mcp_server.server.mail import (
    MAX_ATTACHMENT_CONTENT_BYTES,
    _cap_attachment_content,
)

pytestmark = pytest.mark.unit


def test_small_content_passes_through():
    assert _cap_attachment_content("hello") == "hello"


def test_none_content_passes_through():
    assert _cap_attachment_content(None) is None


def test_oversized_content_replaced_with_byte_sentinel():
    oversized = "a" * (MAX_ATTACHMENT_CONTENT_BYTES + 1)
    result = _cap_attachment_content(oversized)
    assert result != oversized
    assert "too large to inline" in result
    # Reports the actual UTF-8 byte count, not character count.
    assert f"{MAX_ATTACHMENT_CONTENT_BYTES + 1} bytes" in result


def test_multibyte_counted_in_bytes_not_chars():
    # Each "€" is 3 UTF-8 bytes; a string just under the byte cap in characters
    # can still exceed it in bytes.
    char_count = (MAX_ATTACHMENT_CONTENT_BYTES // 3) + 1
    content = "€" * char_count
    # Under the cap by character count, over it by byte count -> capped.
    assert len(content) <= MAX_ATTACHMENT_CONTENT_BYTES
    assert len(content.encode("utf-8")) > MAX_ATTACHMENT_CONTENT_BYTES
    assert "too large to inline" in _cap_attachment_content(content)
