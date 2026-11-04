"""Unstructured Table elements keep their grid instead of being flattened.

A Table element's ``text`` is every cell run together as prose; the row/column
association only survives in ``metadata.text_as_html``. Dropping it turns a
questionnaire into an unsearchable wall of words, so the processor renders the
HTML as a markdown table.
"""

import pathlib
from typing import Any

import httpx
import pytest

from nextcloud_mcp_server.document_processors.unstructured import UnstructuredProcessor

pytestmark = pytest.mark.unit


def _mock_post(mocker, elements: list[dict[str, Any]]):
    """Patch httpx.AsyncClient so ``post`` returns ``elements`` as JSON."""
    response = mocker.Mock(spec=httpx.Response)
    response.json.return_value = elements
    response.raise_for_status.return_value = None

    client = mocker.AsyncMock(spec=httpx.AsyncClient)
    client.post.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    mocker.patch.object(httpx, "AsyncClient", return_value=client)
    return client


TABLE_HTML = (
    "<table>"
    "<tr><td>Security Domain</td><td>Answer</td></tr>"
    "<tr><td>Certifications</td><td>ISO 27001</td></tr>"
    "</table>"
)


async def test_table_element_renders_as_markdown_table(mocker):
    """text_as_html becomes a markdown table, not the flattened cell prose."""
    _mock_post(
        mocker,
        [
            {"type": "Title", "text": "Questionnaire"},
            {
                "type": "Table",
                # What the API puts in ``text``: cells with the grid lost.
                "text": "Security Domain Answer Certifications ISO 27001",
                "metadata": {"text_as_html": TABLE_HTML},
            },
        ],
    )
    processor = UnstructuredProcessor(api_url="http://test:8000")

    result = await processor.process(b"x", "application/pdf", "q.pdf")

    assert "| Security Domain | Answer |" in result.text
    assert "| Certifications | ISO 27001 |" in result.text
    # The flattened form must not also be emitted -- it would double the tokens
    # and give the embedder a contradictory second copy of the same rows.
    assert "Security Domain Answer Certifications" not in result.text
    assert result.metadata["tables_as_markdown"] == 1
    assert result.metadata["parse_mode"] == "markdown"


async def test_untabled_document_stays_text_only(mocker):
    """No table -> unchanged behaviour, and parse_mode still says text_only."""
    _mock_post(
        mocker,
        [
            {"type": "Title", "text": "Contract"},
            {"type": "NarrativeText", "text": "This agreement is made between."},
        ],
    )
    processor = UnstructuredProcessor(api_url="http://test:8000")

    result = await processor.process(b"x", "application/msword", "c.doc")

    assert result.text == "Contract\n\nThis agreement is made between."
    assert result.metadata["tables_as_markdown"] == 0
    assert result.metadata["parse_mode"] == "text_only"


async def test_table_element_without_html_uses_its_text(mocker):
    """A Table carrying no text_as_html must still contribute its cells."""
    _mock_post(
        mocker,
        [{"type": "Table", "text": "Domain Answer Certifications ISO 27001"}],
    )
    processor = UnstructuredProcessor(api_url="http://test:8000")

    result = await processor.process(b"x", "application/pdf", "t.pdf")

    assert result.text == "Domain Answer Certifications ISO 27001"
    assert result.metadata["tables_as_markdown"] == 0
    assert result.metadata["parse_mode"] == "text_only"


async def test_html_on_a_non_table_element_is_not_rendered_as_a_table(mocker):
    """Only tables are read as grids; anything else keeps its own text."""
    _mock_post(
        mocker,
        [
            {
                "type": "NarrativeText",
                "text": "The agreement is made between the parties.",
                "metadata": {"text_as_html": TABLE_HTML},
            }
        ],
    )
    processor = UnstructuredProcessor(api_url="http://test:8000")

    result = await processor.process(b"x", "application/pdf", "n.pdf")

    assert result.text == "The agreement is made between the parties."
    assert result.metadata["tables_as_markdown"] == 0
    assert result.metadata["parse_mode"] == "text_only"


async def test_markdownify_failure_does_not_report_markdown(mocker):
    """The regex fallback returns prose, so parse_mode must not claim markdown.

    html_to_markdown swallows a markdownify exception and strips tags instead.
    That string is non-empty, so treating "truthy" as "converted" would label
    the exact flattened output this processor exists to avoid as markdown.
    """
    _mock_post(
        mocker,
        [
            {
                "type": "Table",
                "text": "Security Domain Answer",
                "metadata": {"text_as_html": TABLE_HTML},
            }
        ],
    )
    mocker.patch("nextcloud_mcp_server.utils.html.md", side_effect=RuntimeError("boom"))
    processor = UnstructuredProcessor(api_url="http://test:8000")

    result = await processor.process(b"x", "application/pdf", "t.pdf")

    assert result.metadata["tables_as_markdown"] == 0
    assert result.metadata["parse_mode"] == "text_only"
    # The element's own text is preferred over the flattened fallback.
    assert result.text == "Security Domain Answer"


def test_the_shared_helper_does_not_drag_in_the_vector_layer():
    """document_processors must not import vector (issue #877 layering).

    Importing vector.html_processor from a processor runs vector/__init__.py,
    which pulls qdrant-client and langchain-text-splitters onto the document
    stack's import path.
    """
    import nextcloud_mcp_server.utils.html as shared

    assert not any(
        name.startswith("nextcloud_mcp_server.vector")
        for name in dir(shared)
        if not name.startswith("_")
    )
    source = pathlib.Path(shared.__file__).read_text()
    assert "nextcloud_mcp_server.vector" not in source


async def test_unconvertible_table_html_falls_back_to_text(mocker):
    """An empty/garbage text_as_html must not lose the element's text."""
    _mock_post(
        mocker,
        [
            {
                "type": "Table",
                "text": "fallback cells",
                "metadata": {"text_as_html": "<table></table>"},
            },
        ],
    )
    processor = UnstructuredProcessor(api_url="http://test:8000")

    result = await processor.process(b"x", "application/pdf", "t.pdf")

    assert "fallback cells" in result.text
    assert result.metadata["parse_mode"] == "text_only"
