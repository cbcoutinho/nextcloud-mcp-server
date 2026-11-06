"""Property tests for the shared DAV path/URL encoders.

The behavioural tests that matter live beside the clients that call these
(``test_webdav.py`` for the '#'-truncation 404, ``test_calendar.py`` for the
space caldav rejects outright). These pin the encoders themselves: for *any*
decoded path, encoding is lossless and leaves nothing a transport could read as
a delimiter.
"""

import re
from urllib.parse import unquote

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.client.dav_urls import encode_dav_path, encode_dav_url

pytestmark = pytest.mark.unit

#: What may appear in an encoded path: unreserved characters, the separator,
#: and percent escapes. No '#', '?' or space, which httpx2/caldav would read as
#: a fragment, a query, or reject outright.
_ENCODED = re.compile(r"[A-Za-z0-9_.\-~/%]*")

# Decoded DAV paths never contain the authority separator, which is what
# ``encode_dav_url`` keys on to tell a URL from a bare path.
paths = st.text().filter(lambda p: "://" not in p)


@given(paths)
@example("already%20encoded.pdf")  # a literal '%' is data: encoded exactly once
@example("notes/draft #1?.md")  # '#' and '?' are characters, not delimiters
@example("a/b c.pdf")
def test_encode_dav_path_is_lossless_and_transport_safe(path):
    encoded = encode_dav_path(path)

    assert unquote(encoded) == path
    assert _ENCODED.fullmatch(encoded)
    assert encoded.count("/") == path.count("/")


@st.composite
def absolute_urls(draw):
    """A decoded absolute DAV URL and the authority that must survive encoding."""
    scheme = draw(st.sampled_from(["http", "https"]))
    host = draw(
        st.from_regex(r"[a-z0-9][a-z0-9.-]{0,30}(:[0-9]{1,5})?", fullmatch=True)
    )
    path = draw(st.text())
    return f"{scheme}://{host}/", path


@given(absolute_urls())
@example(("https://cloud.example.org:8443/", "remote.php/dav/calendars/A B/"))
@example(("https://h/", "remote.php/dav/calendars/u/personal/evt?x.ics"))
def test_encode_dav_url_keeps_the_authority_and_encodes_only_the_path(url):
    authority, path = url

    encoded = encode_dav_url(authority + path)

    assert encoded.startswith(authority)
    rest = encoded[len(authority) :]
    assert unquote(rest) == path
    assert _ENCODED.fullmatch(rest)


@given(paths)
@example("/remote.php/dav/calendars/u/personal/evt#1.ics")
@example("/remote.php/dav/calendars/a%b/")
def test_encode_dav_url_treats_a_bare_path_like_encode_dav_path(path):
    """What caldav hands back from a parsed href has no authority."""
    assert encode_dav_url(path) == encode_dav_path(path)
