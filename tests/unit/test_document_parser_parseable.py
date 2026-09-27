"""Which types nc_webdav_read_file parses on read."""

import pytest

from nextcloud_mcp_server.utils.document_parser import is_parseable_document

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "content_type", ["text/plain", "text/markdown", "text/csv; charset=utf-8"]
)
def test_text_is_returned_as_is_not_parsed(content_type):
    """A processor reads text for indexing; reading the file stays raw."""
    assert is_parseable_document(content_type) is False


def test_documents_with_a_processor_are_parsed():
    assert is_parseable_document("application/pdf") is True


def test_types_without_a_processor_are_not():
    assert is_parseable_document("image/x-nothing") is False
    assert is_parseable_document(None) is False
