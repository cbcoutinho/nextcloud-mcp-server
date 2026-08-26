"""Parsing of the trash-bin and version collections.

Both live on dedicated DAV endpoints and answer with a PROPFIND multistatus.
The collection itself shows up as a response element and has to be skipped,
otherwise it would be reported as a deleted file or a stored version.
"""

import pytest

from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit


def _client() -> WebDAVClient:
    """The parsers only read the response; skip __init__."""
    return WebDAVClient.__new__(WebDAVClient)


class TestPropfindBody:
    def test_requests_every_property(self):
        body = _client()._propfind_body(WebDAVClient._TRASH_PROPS)
        assert "trashbin-filename" in body
        assert "trashbin-original-location" in body
        assert "getcontentlength" in body

    def test_uses_the_right_namespace_prefix(self):
        body = _client()._propfind_body(WebDAVClient._VERSION_PROPS)
        # DAV: properties use d:, Nextcloud's own use nc:
        assert "<d:getcontentlength />" in body
        assert "<nc:version-label />" in body

    def test_is_well_formed(self):
        from lxml import etree

        body = _client()._propfind_body(WebDAVClient._TRASH_PROPS)
        etree.fromstring(body.encode("utf-8"))  # raises if malformed
