"""Unit tests for the shared mail content reconstruction and listing window.

``build_mail_content`` is the single source of truth for index-time and
query-time chunk offsets; these tests pin the exact layout so a change to the
separators or header order can't silently misalign every indexed message.
``list_index_window`` is the equivalent for the scanner/verifier listing.
"""

from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.vector.mail_content import (
    MAIL_SCAN_MAX_PER_MAILBOX,
    build_mail_content,
    format_mail_addresses,
    list_index_window,
)

pytestmark = pytest.mark.unit


async def test_list_index_window_pins_limit_and_singleton_view():
    """The window must request singleton view — threaded hides every reply.

    The Mail app coerces any view that isn't literally "singleton" to its
    threaded view, which returns only the newest message per thread.
    """
    mail_client = AsyncMock()
    mail_client.list_messages.return_value = [{"databaseId": 1}]

    result = await list_index_window(mail_client, 10)

    assert result == [{"databaseId": 1}]
    mail_client.list_messages.assert_awaited_once_with(
        10,
        limit=MAIL_SCAN_MAX_PER_MAILBOX,
        search_filter=None,
        view="singleton",
    )


async def test_list_index_window_passes_filter_through():
    mail_client = AsyncMock()
    mail_client.list_messages.return_value = []

    await list_index_window(mail_client, 10, "tags:7")

    assert mail_client.list_messages.await_args.kwargs["search_filter"] == "tags:7"


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
