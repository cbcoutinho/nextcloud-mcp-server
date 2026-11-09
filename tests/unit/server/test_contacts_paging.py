"""Paging over a large addressbook.

``total_count`` deliberately reports the addressbook size rather than the page
size, so a caller can tell whether more contacts remain.
"""

import pytest

from nextcloud_mcp_server.server.contacts import _page

pytestmark = pytest.mark.unit

CONTACTS = list(range(10))


class TestPaging:
    def test_no_limit_returns_everything(self):
        assert _page(CONTACTS, None, 0) == CONTACTS

    def test_limit_truncates(self):
        assert _page(CONTACTS, 3, 0) == [0, 1, 2]

    def test_offset_skips(self):
        assert _page(CONTACTS, 3, 3) == [3, 4, 5]

    def test_pages_do_not_overlap(self):
        first = _page(CONTACTS, 4, 0)
        second = _page(CONTACTS, 4, 4)
        assert set(first).isdisjoint(second)

    def test_offset_past_the_end_is_empty(self):
        assert _page(CONTACTS, 4, 99) == []

    def test_negative_offset_is_clamped(self):
        assert _page(CONTACTS, 2, -5) == [0, 1]

    def test_negative_limit_yields_nothing(self):
        assert _page(CONTACTS, -1, 0) == []
