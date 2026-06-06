"""Unit tests for the page-aware chunker routing decision (processor.py)."""

import pytest

from nextcloud_mcp_server.vector.processor import should_use_page_aware

pytestmark = pytest.mark.unit

_BOUNDARIES = [{"page": 1, "start_offset": 0, "end_offset": 10}]


class TestShouldUsePageAware:
    """Cover the (doc_type, page_boundaries, page_aware_setting) matrix."""

    def test_pdf_with_boundaries_and_enabled_uses_page_aware(self):
        assert (
            should_use_page_aware(
                page_aware_enabled=True,
                doc_type="file",
                page_boundaries=_BOUNDARIES,
            )
            is True
        )

    def test_empty_boundaries_falls_back_to_char_based(self):
        """Empty list carries no pages -> char-based path."""
        assert (
            should_use_page_aware(
                page_aware_enabled=True, doc_type="file", page_boundaries=[]
            )
            is False
        )

    def test_none_boundaries_falls_back_to_char_based(self):
        assert (
            should_use_page_aware(
                page_aware_enabled=True, doc_type="file", page_boundaries=None
            )
            is False
        )

    @pytest.mark.parametrize("doc_type", ["note", "deck_card", "news_item"])
    def test_non_file_doc_types_never_page_aware(self, doc_type):
        """Only paginated files are page-aware, even with boundaries present."""
        assert (
            should_use_page_aware(
                page_aware_enabled=True,
                doc_type=doc_type,
                page_boundaries=_BOUNDARIES,
            )
            is False
        )

    def test_disabled_setting_forces_char_based(self):
        assert (
            should_use_page_aware(
                page_aware_enabled=False,
                doc_type="file",
                page_boundaries=_BOUNDARIES,
            )
            is False
        )
