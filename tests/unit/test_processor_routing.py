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


class TestOcrChunkBboxes:
    """`_ocr_chunk_bboxes` attributes OCR block bboxes to chunks by char-span overlap."""

    @staticmethod
    def _chunk(start, end, page_number=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            start_offset=start, end_offset=end, page_number=page_number
        )

    @staticmethod
    def _span(start, end, bbox, page=None):
        s = {"start_offset": start, "end_offset": end, "bbox": bbox}
        if page is not None:
            s["page"] = page
        return s

    def test_single_block_per_chunk(self):
        from nextcloud_mcp_server.vector.processor import _ocr_chunk_bboxes

        chunks = [self._chunk(0, 10), self._chunk(10, 20)]
        spans = [
            self._span(0, 8, [0.1, 0.1, 0.4, 0.2]),
            self._span(10, 18, [0.1, 0.3, 0.5, 0.4]),
        ]
        out = _ocr_chunk_bboxes(chunks, spans)
        assert out == {0: [(0.1, 0.1, 0.4, 0.2)], 1: [(0.1, 0.3, 0.5, 0.4)]}

    def test_chunk_spanning_two_blocks_gets_two_bboxes(self):
        from nextcloud_mcp_server.vector.processor import _ocr_chunk_bboxes

        # One chunk [0,20) overlaps both blocks -> two bboxes in reading order.
        chunks = [self._chunk(0, 20)]
        spans = [
            self._span(0, 8, [0.1, 0.1, 0.4, 0.2]),
            self._span(10, 18, [0.1, 0.3, 0.5, 0.4]),
        ]
        out = _ocr_chunk_bboxes(chunks, spans)
        assert out == {0: [(0.1, 0.1, 0.4, 0.2), (0.1, 0.3, 0.5, 0.4)]}

    def test_no_overlap_yields_empty(self):
        from nextcloud_mcp_server.vector.processor import _ocr_chunk_bboxes

        # Chunk [50,60) overlaps no block -> omitted (pymupdf fallback territory).
        out = _ocr_chunk_bboxes([self._chunk(50, 60)], [self._span(0, 8, [0, 0, 1, 1])])
        assert out == {}

    def test_page_guard_excludes_block_from_other_page(self):
        from nextcloud_mcp_server.vector.processor import _ocr_chunk_bboxes

        # A chunk assigned page 2 whose char range also overlaps a page-1 block
        # (possible when a chunk straddles a page break, e.g. the char-based
        # chunker). The page-1 box must NOT be attributed — it would render on
        # page 2 at page-1 coordinates. Only the page-2 block survives.
        chunks = [self._chunk(0, 20, page_number=2)]
        spans = [
            self._span(0, 8, [0.1, 0.1, 0.4, 0.2], page=1),
            self._span(10, 18, [0.1, 0.3, 0.5, 0.4], page=2),
        ]
        out = _ocr_chunk_bboxes(chunks, spans)
        assert out == {0: [(0.1, 0.3, 0.5, 0.4)]}
