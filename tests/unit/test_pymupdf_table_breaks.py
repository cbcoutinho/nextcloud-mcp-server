"""Table cells keep their words searchable across a rendered line wrap."""

import pytest

from nextcloud_mcp_server.document_processors.pymupdf import (
    PyMuPDFProcessor,
    _unwrap_table_cell_breaks,
)

pytestmark = pytest.mark.unit


def test_break_inside_a_cell_becomes_a_space():
    """ "ISO<br>27001" is unfindable by anyone searching for "ISO 27001"."""
    row = "|Certifications|Do you<br>hold ISO<br>27001?|Yes|"

    assert _unwrap_table_cell_breaks(row) == (
        "|Certifications|Do you hold ISO 27001?|Yes|"
    )


@pytest.mark.parametrize("tag", ["<br>", "<br/>", "<br />", "<BR>", "<Br />"])
def test_every_spelling_of_the_tag_is_handled(tag):
    assert _unwrap_table_cell_breaks(f"| a{tag}b |") == "| a b |"


def test_prose_outside_a_table_is_left_alone():
    """A document explaining HTML should keep saying <br>."""
    text = "The <br> tag inserts a line break.\n\n| a<br>b |"

    assert _unwrap_table_cell_breaks(text) == (
        "The <br> tag inserts a line break.\n\n| a b |"
    )


def test_text_without_breaks_is_returned_unchanged():
    text = "# Heading\n\n| a | b |\n"

    assert _unwrap_table_cell_breaks(text) is text


def test_page_boundaries_match_the_rewritten_text():
    """The offsets are taken after the rewrite, so highlights stay on the words.

    Rewriting the text after measuring would leave every offset past the first
    table pointing several characters too far right.
    """
    processor = PyMuPDFProcessor(extract_images=False)
    metadata: dict = {}
    chunks = [
        {"text": "| a<br>b |\n", "metadata": {"page": 1}},
        {"text": "second page\n", "metadata": {"page": 2}},
    ]

    text = processor._build_text_and_metadata(chunks, None, metadata)

    assert text == "| a b |\nsecond page\n"
    boundaries = metadata["page_boundaries"]
    assert boundaries[0]["end_offset"] == len("| a b |\n")
    assert boundaries[1]["start_offset"] == boundaries[0]["end_offset"]
    assert boundaries[-1]["end_offset"] == len(text)
    for span in boundaries:
        assert text[span["start_offset"] : span["end_offset"]]
