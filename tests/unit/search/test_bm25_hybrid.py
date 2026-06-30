"""Unit tests for BM25 hybrid search algorithm."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client import models

from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm


@pytest.mark.unit
def test_bm25_hybrid_initialization_default():
    """Test BM25HybridSearchAlgorithm initializes with default RRF fusion."""
    algo = BM25HybridSearchAlgorithm()

    assert algo.score_threshold == 0.0
    assert algo.fusion == models.Fusion.RRF
    assert algo.fusion_name == "rrf"
    assert algo.name == "bm25_hybrid"


@pytest.mark.unit
def test_bm25_hybrid_initialization_with_rrf():
    """Test BM25HybridSearchAlgorithm initializes with explicit RRF fusion."""
    algo = BM25HybridSearchAlgorithm(score_threshold=0.5, fusion="rrf")

    assert algo.score_threshold == 0.5
    assert algo.fusion == models.Fusion.RRF
    assert algo.fusion_name == "rrf"


@pytest.mark.unit
def test_bm25_hybrid_initialization_with_dbsf():
    """Test BM25HybridSearchAlgorithm initializes with DBSF fusion."""
    algo = BM25HybridSearchAlgorithm(score_threshold=0.7, fusion="dbsf")

    assert algo.score_threshold == 0.7
    assert algo.fusion == models.Fusion.DBSF
    assert algo.fusion_name == "dbsf"


@pytest.mark.unit
def test_bm25_hybrid_invalid_fusion_raises_error():
    """Test BM25HybridSearchAlgorithm raises ValueError for invalid fusion."""
    with pytest.raises(ValueError) as exc_info:
        BM25HybridSearchAlgorithm(fusion="invalid")

    assert "Invalid fusion algorithm 'invalid'" in str(exc_info.value)
    assert "Must be 'rrf' or 'dbsf'" in str(exc_info.value)


@pytest.mark.unit
def test_bm25_hybrid_requires_vector_db():
    """Test BM25HybridSearchAlgorithm reports it requires vector database."""
    algo = BM25HybridSearchAlgorithm()
    assert algo.requires_vector_db is True


def _make_search_deps(monkeypatch, *, dense_enabled: bool):
    """Stub the embedding / BM25 / Qdrant / settings deps of ``search()`` and
    return ``(embed, qdrant)`` mocks. ``dense_enabled`` selects hybrid (True) vs
    keyword (False) mode; everything else is identical, so both fixtures below
    share this one builder."""
    embed = AsyncMock(return_value=([0.1, 0.2, 0.3], 7))
    svc = MagicMock()
    svc.embed_with_usage = embed
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_embedding_service", lambda: svc
    )

    bm25 = MagicMock()
    bm25.encode_async = AsyncMock(return_value={"indices": [1], "values": [0.5]})
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_bm25_service",
        AsyncMock(return_value=bm25),
    )

    qdrant = MagicMock()
    empty = MagicMock()
    empty.points = []
    qdrant.query_points = AsyncMock(return_value=empty)
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
        AsyncMock(return_value=qdrant),
    )

    settings = MagicMock()
    settings.dense_enabled = dense_enabled
    settings.get_collection_name.return_value = "test_collection"
    settings.get_embedding_provider_family.return_value = "mistral"
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_settings", lambda: settings
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.build_base_filter_conditions",
        lambda **kwargs: [],
    )
    return embed, qdrant


@pytest.fixture
def patched_search(monkeypatch):
    """Hybrid-mode deps; returns the embed mock so tests can assert how often the
    query was embedded."""
    embed, _ = _make_search_deps(monkeypatch, dense_enabled=True)
    return embed


@pytest.mark.unit
async def test_query_embedded_and_metered_once_across_doc_types(patched_search):
    """nc_semantic_search calls search() once per doc_type on one instance with
    the same query; the dense embedding (and its billed token count) must be
    computed exactly once, not once per type."""
    embed = patched_search
    algo = BM25HybridSearchAlgorithm()

    for dtype in ("note", "file", "deck_card"):
        await algo.search(query="hello", user_id="alice", doc_type=dtype)

    assert embed.await_count == 1  # embedded once, not 3×
    assert (
        algo.query_token_count == 7
    )  # single query's token count, not summed/overwritten
    assert algo.query_embedding == [0.1, 0.2, 0.3]


@pytest.mark.unit
async def test_different_query_invalidates_cache(patched_search):
    """A different query string re-embeds (and re-meters)."""
    embed = patched_search
    algo = BM25HybridSearchAlgorithm()

    await algo.search(query="hello", user_id="alice")
    await algo.search(query="world", user_id="alice")

    assert embed.await_count == 2


@pytest.mark.unit
async def test_hybrid_query_uses_dense_prefetch_and_fusion(patched_search, monkeypatch):
    """Regression guard: hybrid mode still fuses a dense + sparse prefetch."""
    qdrant = MagicMock()
    empty = MagicMock()
    empty.points = []
    qdrant.query_points = AsyncMock(return_value=empty)
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
        AsyncMock(return_value=qdrant),
    )

    algo = BM25HybridSearchAlgorithm()
    await algo.search(query="hello", user_id="alice")

    kwargs = qdrant.query_points.await_args.kwargs
    assert "prefetch" in kwargs and len(kwargs["prefetch"]) == 2
    assert isinstance(kwargs["query"], models.FusionQuery)


@pytest.fixture
def patched_keyword_search(monkeypatch):
    """Keyword mode (ADR-030): dense disabled. Returns (embed, qdrant) so tests
    can assert the embedding service is never called and the Qdrant call is a
    direct sparse query with no fusion."""
    return _make_search_deps(monkeypatch, dense_enabled=False)


@pytest.mark.unit
async def test_keyword_mode_never_embeds_query(patched_keyword_search):
    """Keyword mode must not contact the embedding service (airgapped)."""
    embed, _ = patched_keyword_search
    algo = BM25HybridSearchAlgorithm()

    await algo.search(query="invoice 2026", user_id="alice")

    assert embed.await_count == 0
    assert algo.query_embedding is None
    assert algo.query_token_count is None


@pytest.mark.unit
async def test_keyword_mode_issues_direct_sparse_query(patched_keyword_search):
    """Keyword mode issues a single sparse query, no dense prefetch / fusion."""
    _, qdrant = patched_keyword_search
    algo = BM25HybridSearchAlgorithm()

    await algo.search(query="invoice 2026", user_id="alice")

    kwargs = qdrant.query_points.await_args.kwargs
    assert kwargs["using"] == "sparse"
    assert isinstance(kwargs["query"], models.SparseVector)
    assert "prefetch" not in kwargs
    assert not isinstance(kwargs["query"], models.FusionQuery)


@pytest.mark.unit
async def test_keyword_mode_search_method_label(patched_keyword_search, monkeypatch):
    """Keyword results are tagged search_method='bm25_keyword'."""
    _, qdrant = patched_keyword_search

    captured: dict = {}

    def fake_build(point, metadata_extras):
        captured.update(metadata_extras)
        return MagicMock()

    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.build_search_result_from_point",
        fake_build,
    )
    response = MagicMock()
    response.points = [MagicMock(score=3.2)]
    qdrant.query_points = AsyncMock(return_value=response)

    algo = BM25HybridSearchAlgorithm()
    results = await algo.search(query="invoice", user_id="alice", limit=5)

    assert results
    assert captured["search_method"] == "bm25_keyword"
