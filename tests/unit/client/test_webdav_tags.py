"""Parsing of the systemtags collection.

Tags come back as a PROPFIND multistatus. The collection itself appears as a
response element and must be skipped, and an entry without an id or display
name is unusable and dropped rather than surfaced with placeholder values.
"""

import pytest

from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit

NS = 'xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"'


def _multistatus(*entries: str) -> bytes:
    return (
        f'<?xml version="1.0"?><d:multistatus {NS}>'
        "<d:response><d:href>/remote.php/dav/systemtags/</d:href></d:response>"
        + "".join(entries)
        + "</d:multistatus>"
    ).encode("utf-8")


def _tag(tag_id: str, name: str, assignable: str = "true") -> str:
    return (
        f"<d:response><d:href>/remote.php/dav/systemtags/{tag_id}</d:href>"
        f"<d:propstat><d:prop><oc:id>{tag_id}</oc:id>"
        f"<oc:display-name>{name}</oc:display-name>"
        f"<oc:user-assignable>{assignable}</oc:user-assignable>"
        "</d:prop></d:propstat></d:response>"
    )


def _parse(payload: bytes) -> list[dict]:
    """Run the client's own extraction over a canned response body."""
    from lxml import etree

    return WebDAVClient._tags_from_multistatus(etree.fromstring(payload))


class TestTagParsing:
    def test_collection_itself_is_skipped(self):
        assert _parse(_multistatus()) == []

    def test_tags_are_returned(self):
        tags = _parse(_multistatus(_tag("7", "Invoice"), _tag("8", "Tax")))
        assert [t["id"] for t in tags] == [7, 8]

    def test_sorted_case_insensitively(self):
        tags = _parse(_multistatus(_tag("1", "zebra"), _tag("2", "Apple")))
        assert [t["name"] for t in tags] == ["Apple", "zebra"]

    def test_assignable_flag(self):
        tags = _parse(_multistatus(_tag("1", "Locked", assignable="false")))
        assert tags[0]["assignable"] is False

    def test_entry_without_id_is_dropped(self):
        broken = (
            "<d:response><d:href>/remote.php/dav/systemtags/9</d:href>"
            "<d:propstat><d:prop>"
            "<oc:display-name>Nameless</oc:display-name>"
            "</d:prop></d:propstat></d:response>"
        )
        assert _parse(_multistatus(broken)) == []

    def test_entry_without_display_name_is_dropped(self):
        broken = (
            "<d:response><d:href>/remote.php/dav/systemtags/9</d:href>"
            "<d:propstat><d:prop><oc:id>9</oc:id>"
            "</d:prop></d:propstat></d:response>"
        )
        assert _parse(_multistatus(broken)) == []

    def test_entry_with_empty_display_name_is_dropped(self):
        """An empty name would be kept and make the sort key ambiguous."""
        assert _parse(_multistatus(_tag("9", ""))) == []

    def test_entry_with_whitespace_display_name_is_dropped(self):
        assert _parse(_multistatus(_tag("9", "   "))) == []


class TestPropfindBody:
    def test_requests_the_tag_properties(self):
        body = WebDAVClient._TAG_PROPFIND
        for prop in ("oc:id", "oc:display-name", "oc:user-assignable"):
            assert prop in body

    def test_is_well_formed(self):
        from lxml import etree

        etree.fromstring(WebDAVClient._TAG_PROPFIND.encode("utf-8"))
