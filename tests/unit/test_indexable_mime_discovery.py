"""Tagged-file discovery covers the office formats, not only PDFs."""

import pytest

from nextcloud_mcp_server.client import _as_mime_tuple
from nextcloud_mcp_server.config import Settings

pytestmark = pytest.mark.unit

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestMimeTupleNormalisation:
    def test_a_bare_string_is_wrapped_not_iterated(self):
        """Iterating it would make startswith match on single letters."""
        assert _as_mime_tuple("application/pdf") == ("application/pdf",)

    def test_none_means_no_filter(self):
        assert _as_mime_tuple(None) == ()

    def test_a_sequence_is_preserved_in_order(self):
        assert _as_mime_tuple([DOCX, XLSX]) == (DOCX, XLSX)

    def test_the_result_drives_startswith_directly(self):
        types = _as_mime_tuple(["application/pdf", DOCX])

        assert "application/pdf".startswith(types)
        assert f"{DOCX}; charset=binary".startswith(types)
        assert not "image/png".startswith(types)


class TestIndexableSetting:
    def test_the_default_covers_pdf_office_and_outlook(self):
        types = Settings().indexable_mime_types

        assert "application/pdf" in types
        assert "application/msword" in types
        assert DOCX in types
        assert "application/vnd.ms-excel" in types
        assert XLSX in types
        assert "application/vnd.ms-outlook" in types

    def test_whitespace_and_blank_entries_are_dropped(self):
        settings = Settings(
            vector_sync_indexable_mime_types="application/pdf, ,  text/plain,"
        )

        assert settings.indexable_mime_types == ("application/pdf", "text/plain")

    def test_an_operator_can_narrow_it_back_to_pdf_only(self):
        settings = Settings(vector_sync_indexable_mime_types="application/pdf")

        assert settings.indexable_mime_types == ("application/pdf",)
