"""Unit tests for the shared DAV path/URL encoders.

The behavioural tests that matter live beside the clients that call these
(``test_webdav.py`` for the '#'-truncation 404, ``test_calendar.py`` for the
space caldav rejects outright). These pin the encoders themselves.
"""

import pytest

from nextcloud_mcp_server.client.dav_urls import encode_dav_path, encode_dav_url

pytestmark = pytest.mark.unit


def test_encode_dav_path_encodes_exactly_once():
    """Pins the decoded-input precondition: a literal '%' becomes '%25', so an
    already-encoded path passed in error would double-encode (caught here)."""
    assert encode_dav_path("already%20encoded.pdf") == "already%2520encoded.pdf"


def test_encode_dav_path_keeps_separators_and_encodes_delimiters():
    """'/' separates; '#' and '?' are literal characters in a DAV path."""
    assert encode_dav_path("a/b c.pdf") == "a/b%20c.pdf"
    assert encode_dav_path("notes/draft #1?.md") == "notes/draft%20%231%3F.md"


def test_encode_dav_url_preserves_the_authority():
    """Scheme, host and port must survive; only the path is encoded."""
    assert (
        encode_dav_url("https://cloud.example.org:8443/remote.php/dav/calendars/A B/")
        == "https://cloud.example.org:8443/remote.php/dav/calendars/A%20B/"
    )
    # A bare path (what caldav hands back from a parsed href) has no authority.
    assert (
        encode_dav_url("/remote.php/dav/calendars/A B/")
        == "/remote.php/dav/calendars/A%20B/"
    )
    # Decoded input, so a literal '%' encodes to '%25' -- exactly once.
    assert (
        encode_dav_url("/remote.php/dav/calendars/a%b/")
        == "/remote.php/dav/calendars/a%25b/"
    )


def test_encode_dav_url_does_not_truncate_at_a_fragment_or_query_marker():
    """``urlsplit`` would read these as delimiters and drop the remainder."""
    assert (
        encode_dav_url("/remote.php/dav/calendars/u/personal/evt#1.ics")
        == "/remote.php/dav/calendars/u/personal/evt%231.ics"
    )
    assert (
        encode_dav_url("https://h/remote.php/dav/calendars/u/personal/evt?x.ics")
        == "https://h/remote.php/dav/calendars/u/personal/evt%3Fx.ics"
    )
