"""Redacted views on the Astrolabe HTTP surface (ADR-038). Synthetic names only.

Drives the real Starlette handlers, like ``test_search_rerank_api.py`` and
``test_management_chunk_context_endpoint.py``, so the HTTP contract is pinned:
the capability gate, request validation, which fields are redacted, and that a
detection failure returns nothing (503) rather than unredacted text.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import get_chunk_context, unified_search
from nextcloud_mcp_server.providers.ner import NerError
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit

_V = "nextcloud_mcp_server.api.visualization"


def _settings(*, redaction="optional", gateway="https://gw.example"):
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.search_rerank_enabled = False
    settings.content_redaction = redaction
    settings.embedding_gateway_url = gateway
    settings.ner_model = "local/test-ner"
    settings.usage_metering_enabled = False
    return settings


def _row(**overrides) -> SearchResult:
    values = dict(
        id="42",
        doc_type="file",
        title="Letter to Karen Smith",
        excerpt="Jane Doe wrote about Karen Smith. Smith replied.",
        score=0.5,
        metadata={"path": "HR/Karen Smith/letter.pdf"},
        person_names=["jane doe", "karen smith"],
        title_person_names=["karen smith"],
    )
    values.update(overrides)
    return SearchResult(**values)


def _ner(side_effect=None, names=None, *, where="nextcloud_mcp_server.redaction"):
    client = MagicMock()
    client.model = "local/test-ner"
    client.detect = AsyncMock(side_effect=side_effect, return_value=names or [])
    return patch(f"{where}.get_ner_client", new=AsyncMock(return_value=client)), client


def _search(body, *, rows, settings=None, ner_side_effect=None, ner_names=None):
    algo = MagicMock()
    algo.search = AsyncMock(return_value=rows)
    algo.query_token_count = 0
    algo.query_embedding = None
    ner_patch, ner_client = _ner(ner_side_effect, ner_names)
    app = Starlette(routes=[Route("/api/v1/search", unified_search, methods=["POST"])])
    app.state.oauth_context = {"config": {"nextcloud_host": "https://nc.example"}}
    with (
        patch(f"{_V}.get_settings", return_value=settings or _settings()),
        patch(
            f"{_V}.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(f"{_V}.BM25HybridSearchAlgorithm", return_value=algo),
        patch(
            f"{_V}.get_user_client_basic_auth",
            new=AsyncMock(side_effect=NotProvisionedError("not provisioned")),
        ),
        ner_patch,
    ):
        return TestClient(app).post("/api/v1/search", json=body), ner_client


# ── POST /api/v1/search ─────────────────────────────────────────────────


def test_search_redacts_title_excerpt_and_path_keeping_subject():
    resp, ner_client = _search(
        {"query": "letter", "redact": True, "keep_names": ["Jane Doe"]},
        rows=[_row()],
    )

    assert resp.status_code == 200
    data = resp.json()
    (row,) = data["results"]
    assert row["title"] == "Letter to [PERSON_1]"
    assert row["excerpt"] == "Jane Doe wrote about [PERSON_1]. [PERSON_2] replied."
    assert row["path"] == "HR/[PERSON_1]/letter.pdf"
    assert data["redaction"]["applied"] is True
    assert data["redaction"]["kept_names"] == ["Jane Doe"]
    ner_client.detect.assert_not_called()  # names were stored at ingest


def test_search_default_is_unredacted():
    resp, ner_client = _search({"query": "letter"}, rows=[_row()])

    data = resp.json()
    assert data["results"][0]["title"] == "Letter to Karen Smith"
    assert "redaction" not in data
    ner_client.detect.assert_not_called()


def test_search_unscanned_row_detection_failure_returns_503():
    resp, _ = _search(
        {"query": "letter", "redact": True},
        rows=[_row(person_names=None, title_person_names=None)],
        ner_side_effect=NerError("NER endpoint returned HTTP 503"),
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == "redaction_failed"
    assert "Karen" not in resp.text


@pytest.mark.parametrize(
    "settings",
    [_settings(redaction="off"), _settings(gateway=None)],
    ids=["off", "no-gateway"],
)
def test_search_redact_unavailable_returns_422(settings):
    resp, _ = _search(
        {"query": "letter", "redact": True}, rows=[_row()], settings=settings
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "redaction_not_available"


@pytest.mark.parametrize(
    "body",
    [
        {"redact": "yes"},
        {"redact": True, "keep_names": "Jane Doe"},
        {"redact": True, "keep_names": ["x" * 201]},
        {"redact": True, "keep_names": ["a"] * 51},
    ],
    ids=["redact-not-bool", "keep-not-list", "name-too-long", "too-many-names"],
)
def test_search_malformed_redaction_request_returns_400(body):
    resp, _ = _search({"query": "letter", **body}, rows=[_row()])

    assert resp.status_code == 400


# ── GET /api/v1/chunk-context ───────────────────────────────────────────


def _chunk_context(*, query, settings=None, ner_side_effect=None, ner_names=None):
    ctx = MagicMock()
    ctx.chunk_text = "Karen Smith wrote."
    ctx.before_context = "Dear Jane Doe,"
    ctx.after_context = "Smith signed."
    ctx.has_before_truncation = False
    ctx.has_after_truncation = False
    ctx.page_number = 1
    ctx.chunk_index = 0
    ctx.total_chunks = 1
    nc_client = MagicMock()
    nc_client.__aenter__ = AsyncMock(return_value=nc_client)
    nc_client.__aexit__ = AsyncMock(return_value=None)
    # The handler calls get_ner_client directly, so patch its own reference.
    ner_patch, ner_client = _ner(ner_side_effect, ner_names, where=_V)
    app = Starlette(
        routes=[Route("/api/v1/chunk-context", get_chunk_context, methods=["GET"])]
    )
    app.state.oauth_context = {"config": {"nextcloud_host": "http://localhost:8080"}}
    with (
        patch(f"{_V}.get_settings", return_value=settings or _settings()),
        patch(
            f"{_V}.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", True)),
        ),
        patch(
            f"{_V}.get_user_client_basic_auth", new=AsyncMock(return_value=nc_client)
        ),
        patch(f"{_V}.get_chunk_with_context", new=AsyncMock(return_value=ctx)),
        patch(
            f"{_V}.get_chunk_bbox_and_page_from_qdrant",
            new=AsyncMock(return_value=([[0.1, 0.1, 0.5, 0.2]], 1)),
        ),
        ner_patch,
    ):
        resp = TestClient(app).get(
            f"/api/v1/chunk-context?doc_type=file&doc_id=42&start=0&end=18{query}",
            headers={"Authorization": "Bearer t"},
        )
        return resp, ner_client


def test_chunk_context_redacts_text_and_withholds_bbox():
    resp, _ = _chunk_context(
        query="&redact=true&keep_names=Jane%20Doe",
        ner_names=[{"Karen Smith"}, {"Jane Doe"}, set()],
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["chunk_text"] == "[PERSON_1] wrote."
    assert data["before_context"] == "Dear Jane Doe,"
    assert data["after_context"] == "[PERSON_2] signed."
    assert "chunk_bbox" not in data
    assert data["redaction"]["kept_names"] == ["Jane Doe"]


def test_chunk_context_without_redact_keeps_bbox():
    resp, ner_client = _chunk_context(query="")

    data = resp.json()
    assert data["chunk_text"] == "Karen Smith wrote."
    assert data["chunk_bbox"] == [[0.1, 0.1, 0.5, 0.2]]
    ner_client.detect.assert_not_called()


def test_chunk_context_detection_failure_returns_503():
    resp, _ = _chunk_context(query="&redact=true", ner_side_effect=NerError("timeout"))

    assert resp.status_code == 503
    assert "Karen" not in resp.text


def test_chunk_context_redact_unavailable_returns_422():
    resp, _ = _chunk_context(query="&redact=true", settings=_settings(redaction="off"))

    assert resp.status_code == 422
