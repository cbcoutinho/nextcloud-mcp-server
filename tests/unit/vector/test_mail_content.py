"""Unit tests for the shared mail content reconstruction.

``build_mail_content`` is the single source of truth for index-time and
query-time chunk offsets; these tests pin the exact layout so a change to the
separators or header order can't silently misalign every indexed message.
"""

import pytest

from nextcloud_mcp_server.vector.mail_content import (
    build_mail_content,
    format_mail_addresses,
)

pytestmark = pytest.mark.unit


def test_format_addresses_variants():
    assert (
        format_mail_addresses([{"label": "Alice", "email": "alice@example.com"}])
        == "Alice <alice@example.com>"
    )
    # Email only, label only, label==email, and multiple joined by ", ".
    assert format_mail_addresses([{"email": "bob@example.com"}]) == "bob@example.com"
    assert format_mail_addresses([{"label": "Ops"}]) == "Ops"
    assert format_mail_addresses([{"label": "x@y.z", "email": "x@y.z"}]) == "x@y.z"
    assert format_mail_addresses(None) == ""
    assert (
        format_mail_addresses([{"email": "a@x.io"}, {"label": "B", "email": "b@x.io"}])
        == "a@x.io, B <b@x.io>"
    )


def test_build_mail_content_plain_text_layout():
    message = {
        "subject": "Hello",
        "from": [{"label": "Alice", "email": "alice@example.com"}],
        "to": [{"email": "bob@example.com"}],
        "hasHtmlBody": False,
        "body": "Hi there.",
    }
    assert build_mail_content(message) == (
        "Hello\nFrom: Alice <alice@example.com>\nTo: bob@example.com\n\nHi there."
    )


def test_build_mail_content_includes_cc_and_bcc_when_present():
    message = {
        "subject": "Sync",
        "from": [{"email": "a@x.io"}],
        "to": [{"email": "b@x.io"}],
        "cc": [{"email": "c@x.io"}],
        "bcc": [{"email": "d@x.io"}],
        "hasHtmlBody": False,
        "body": "body",
    }
    assert build_mail_content(message) == (
        "Sync\nFrom: a@x.io\nTo: b@x.io\nCc: c@x.io\nBcc: d@x.io\n\nbody"
    )


def test_build_mail_content_converts_html_body():
    message = {
        "subject": "HTML",
        "from": [{"email": "a@x.io"}],
        "hasHtmlBody": True,
        "body": "<p>Hello <strong>world</strong></p>",
    }
    result = build_mail_content(message)
    # Header preserved; body converted to markdown (no raw tags).
    assert result.startswith("HTML\nFrom: a@x.io\n\n")
    assert "<p>" not in result
    assert "world" in result


def test_build_mail_content_tolerates_empty_fields():
    # No subject/addresses/body (e.g. a 206 partial) -> just the blank-line +
    # empty body, with no spurious header lines.
    assert build_mail_content({}) == "\n\n"
