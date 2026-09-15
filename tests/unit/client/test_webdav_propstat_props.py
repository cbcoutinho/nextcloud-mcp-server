"""Property reads must ignore the 404 propstat block.

A multistatus response carries one propstat per status: the 200 block holds
the values, the 404 block lists properties the server does not have. Reading
across both fabricates empty values for the missing ones.
"""

import pytest
from lxml import etree

from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit

NS = 'xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns"'


def _response(found: str, missing: str = "") -> etree._Element:
    missing_block = (
        f"<d:propstat><d:prop>{missing}</d:prop>"
        "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        if missing
        else ""
    )
    return etree.fromstring(
        f"<d:response {NS}><d:href>/x</d:href>"
        f"<d:propstat><d:prop>{found}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        f"{missing_block}</d:response>".encode("utf-8")
    )


class TestReadProps:
    def test_reads_values_from_the_ok_block(self):
        elem = _response("<d:getcontentlength>42</d:getcontentlength>")
        props = WebDAVClient._read_props(elem, (("size", "{DAV:}getcontentlength"),))
        assert props["size"] == "42"

    def test_missing_property_is_none(self):
        elem = _response("<d:getcontentlength>42</d:getcontentlength>")
        props = WebDAVClient._read_props(
            elem, (("label", "{http://nextcloud.org/ns}version-label"),)
        )
        assert props["label"] is None

    def test_not_found_block_is_ignored(self):
        """The 404 block must not turn an absent property into a value."""
        elem = _response(
            "<d:getcontentlength>42</d:getcontentlength>",
            missing="<nc:version-label/>",
        )
        props = WebDAVClient._read_props(
            elem,
            (
                ("size", "{DAV:}getcontentlength"),
                ("label", "{http://nextcloud.org/ns}version-label"),
            ),
        )
        assert props == {"size": "42", "label": None}


class TestAsInt:
    """DAV numbers arrive as text and may be absent or malformed."""

    def test_parses_a_number(self):
        from nextcloud_mcp_server.server.webdav import _as_int

        assert _as_int("4096") == 4096

    def test_none_stays_none(self):
        from nextcloud_mcp_server.server.webdav import _as_int

        assert _as_int(None) is None

    def test_garbage_costs_the_field_not_the_row(self):
        from nextcloud_mcp_server.server.webdav import _as_int

        assert _as_int("not a number") is None
