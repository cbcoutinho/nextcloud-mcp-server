"""Redacted ``nc_semantic_search`` results (ADR-038). Synthetic names only.

Drives ``_redact_results`` directly: the retrieval pipeline around it needs a
live Qdrant, and what matters here is which names reach the ``Redactor`` and
that every returned field goes through it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.models.semantic import SemanticSearchResult
from nextcloud_mcp_server.providers.ner import NerError
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.server import semantic

pytestmark = pytest.mark.unit

_SETTINGS = SimpleNamespace(ner_model="local/test-ner")


def _hit(**overrides) -> SearchResult:
    values = dict(
        id="42",
        doc_type="file",
        title="Letter to Karen Smith",
        excerpt="Jane Doe wrote about Karen Smith. Smith replied.",
        score=0.03,
        metadata={"path": "HR/Karen Smith/letter.pdf", "chunk_index": 0},
        chunk_start_offset=0,
        chunk_end_offset=48,
    )
    values.update(overrides)
    return SearchResult(**values)


def _row(hit: SearchResult, **overrides) -> SemanticSearchResult:
    values = dict(
        id=int(hit.id),
        doc_type=hit.doc_type,
        title=hit.title,
        excerpt=hit.excerpt,
        score=hit.score,
        relevance=0.5,
        relevance_source="fusion_ordinal",
        chunk_index=0,
        total_chunks=1,
        url="https://nc.example/unredacted-link",
    )
    values.update(overrides)
    return SemanticSearchResult(**values)


@pytest.fixture
def ner(mocker):
    def _install(names_per_text=None, side_effect=None):
        client = MagicMock()
        client.model = "local/test-ner"
        client.detect = AsyncMock(side_effect=side_effect, return_value=names_per_text)
        mocker.patch.object(semantic, "get_ner_client", AsyncMock(return_value=client))
        return client

    return _install


async def test_scanned_rows_use_stored_names_without_calling_ner(ner):
    ner_client = ner()
    hit = _hit(
        person_names=["jane doe", "karen smith"],
        title_person_names=["karen smith"],
    )
    row = _row(hit)

    info = await semantic._redact_results(
        [row],
        [hit],
        keep_names=["Jane Doe"],
        settings=_SETTINGS,
        browser_base="https://nc.example",
    )

    ner_client.detect.assert_not_called()
    assert row.title == "Letter to [PERSON_1]"
    assert row.excerpt == "Jane Doe wrote about [PERSON_1]. [PERSON_2] replied."
    # The deep link is rebuilt from the redacted title and path.
    assert row.url is not None
    assert "Karen" not in row.url and "Smith" not in row.url
    assert info.persons_redacted == 2
    assert info.kept_names == ["Jane Doe"]


async def test_unscanned_rows_and_context_are_detected_live(ner):
    ner_client = ner([{"Karen Smith"}] * 5 + [{"Tom Brown"}])
    hit = _hit(person_names=None)
    row = _row(hit, category="", after_context="Tom Brown was copied.")

    await semantic._redact_results(
        [row], [hit], keep_names=[], settings=_SETTINGS, browser_base=None
    )

    (sent,) = ner_client.detect.await_args.args
    assert "Tom Brown was copied." in sent  # context always goes live
    assert hit.excerpt in sent  # unscanned row goes live
    assert row.after_context == "[PERSON_3] was copied."
    assert "Karen" not in row.excerpt


async def test_live_detection_failure_returns_nothing(ner):
    ner(side_effect=NerError("NER endpoint returned HTTP 503"))
    hit = _hit(person_names=None)
    with pytest.raises(ToolError, match="no search results are returned"):
        await semantic._redact_results(
            [_row(hit)], [hit], keep_names=[], settings=_SETTINGS, browser_base=None
        )


async def test_names_from_one_row_redact_the_others(ner):
    """One Redactor per response: numbering is shared across rows."""
    ner()
    a = _hit(person_names=["karen smith"], title_person_names=[])
    b = _hit(
        id="43",
        title="Minutes",
        excerpt="Smith attended.",
        person_names=[],
        title_person_names=[],
    )
    rows = [_row(a), _row(b)]
    await semantic._redact_results(
        rows, [a, b], keep_names=[], settings=_SETTINGS, browser_base=None
    )
    assert rows[1].excerpt == "[PERSON_2] attended."
