"""Table cells keep their words searchable across a rendered line wrap."""

import time

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


def test_consecutive_breaks_collapse_to_one_space():
    """Each tag substitutes independently, so a run would otherwise remain."""
    assert _unwrap_table_cell_breaks("| a<br><br>b |") == "| a b |"


def test_space_collapsing_is_confined_to_table_rows():
    """Prose keeps its own spacing -- only the rewritten rows are normalised."""
    text = "Indented  prose  keeps  spacing.\n\n| a<br><br>b |"

    assert _unwrap_table_cell_breaks(text) == (
        "Indented  prose  keeps  spacing.\n\n| a b |"
    )


def test_an_unrelated_cell_in_the_same_row_keeps_its_double_space():
    """The collapse must not reach past the break it is repairing.

    Substituting each tag and then collapsing runs operated on the whole line,
    so a legitimate double space in a different cell of the same row was eaten
    as collateral.
    """
    row = "| kept  spacing | a<br><br>b | more  spacing |"

    assert _unwrap_table_cell_breaks(row) == ("| kept  spacing | a b | more  spacing |")


def test_spaces_around_a_break_are_absorbed_into_the_one_space():
    assert _unwrap_table_cell_breaks("| a <br> b |") == "| a b |"


def test_a_long_space_run_without_a_break_is_not_quadratic():
    """A padded row must not make the regex backtrack over its whole run.

    A `[ \\t]*` prefix lets the engine eat a space run, fail to find a `<br>`,
    then retry one character shorter -- quadratic in the run length, and
    rendered tables are mostly padding (python:S8786).
    """
    padded = "| " + " " * 20000 + "|\n| a<br>b |"

    start = time.perf_counter()
    result = _unwrap_table_cell_breaks(padded)
    elapsed = time.perf_counter() - start

    assert result.endswith("| a b |")
    assert " " * 20000 in result, "the padding itself must be left alone"
    # Linear scanning finishes in microseconds; the quadratic form took seconds
    # on this input. A whole second is a generous ceiling that still fails loudly.
    assert elapsed < 1.0, f"took {elapsed:.2f}s -- regex is backtracking"


def test_a_row_without_a_break_keeps_its_own_double_spaces():
    """The early-out is per page, so a sibling row must not be rewritten.

    One <br> anywhere on the page used to send every other table row through
    the space collapse, silently eating double spaces the document contains.
    """
    text = "| kept  spacing | here |\n| a<br><br>b |"

    assert _unwrap_table_cell_breaks(text) == "| kept  spacing | here |\n| a b |"


def test_a_page_with_no_breaks_at_all_is_untouched():
    text = "| kept  spacing |\n| and  here |"

    assert _unwrap_table_cell_breaks(text) is text


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
