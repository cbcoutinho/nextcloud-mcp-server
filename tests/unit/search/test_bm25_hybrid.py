"""Unit tests for BM25 hybrid search algorithm."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client import models

from nextcloud_mcp_server.search.bm25_hybrid import (
    DOCUMENT_PREFETCH_FACTOR,
    MAX_DOCUMENT_PREFETCH,
    BM25HybridSearchAlgorithm,
)


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


def _make_search_deps(monkeypatch):
    """Stub the embedding / BM25 / Qdrant / settings deps of ``search()`` and
    return ``(embed, qdrant)`` mocks. The query always fuses dense + sparse
    (there is no keyword-only mode), so a single builder serves every test."""
    embed = AsyncMock(return_value=([0.1, 0.2, 0.3], 7))
    svc = MagicMock()
    svc.embed_with_usage = embed
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_provider", lambda: svc
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
    settings.get_collection_name.return_value = "test_collection"
    settings.get_embedding_provider_family.return_value = "mistral"
    # Must be a real int: search() builds models.Rrf(k=...), a pydantic-validated
    # int field. An unconfigured MagicMock does NOT raise here — pydantic coerces
    # it via MagicMock.__int__, which returns 1 — so these tests would silently
    # exercise k=1 (the most degenerate ranking constant) instead of the
    # configured default. Pin it explicitly.
    settings.vector_search_rrf_k = 60
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
    embed, _ = _make_search_deps(monkeypatch)
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
    # RRF now goes out as RrfQuery so the ranking constant is explicit rather
    # than Qdrant's k=2 default (see TestFusionRankingConstant).
    assert isinstance(kwargs["query"], models.RrfQuery)
    # Pin the k that actually reaches Qdrant, not just the query type. Rrf.k is
    # a pydantic int field, so a stubbed-but-unset settings attribute coerces
    # silently (MagicMock.__int__ -> 1) rather than raising; without this
    # assertion the test would still pass while exercising k=1.
    assert kwargs["query"].rrf.k == 60


@pytest.mark.unit
async def test_search_method_label_is_always_bm25_hybrid(patched_search, monkeypatch):
    """Results are tagged search_method='bm25_hybrid_<fusion>' — the query always
    fuses dense + sparse, so there is no keyword-only label anymore."""
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
    qdrant = MagicMock()
    qdrant.query_points = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
        AsyncMock(return_value=qdrant),
    )

    algo = BM25HybridSearchAlgorithm()
    results = await algo.search(query="invoice", user_id="alice", limit=5)

    assert results
    assert captured["search_method"] == "bm25_hybrid_rrf"


class TestFusionRankingConstant:
    """RRF must use an explicit ranking constant, not Qdrant's k=2 default.

    Qdrant's plain ``FusionQuery(fusion=RRF)`` scores 1/(rank+2), so adjacent
    ranks differ by 33% and a point ranked top by ONE retriever (0.5) beats a
    point ranked 3rd by BOTH (0.2+0.2=0.4) — inverting the purpose of fusion.
    """

    @pytest.mark.unit
    def test_rrf_carries_the_configured_k(self):
        settings = MagicMock()
        settings.vector_search_rrf_k = 60

        query = BM25HybridSearchAlgorithm(fusion="rrf")._build_fusion_query(settings)

        assert isinstance(query, models.RrfQuery)
        assert query.rrf.k == 60

    @pytest.mark.unit
    def test_rrf_k_is_configurable(self):
        settings = MagicMock()
        settings.vector_search_rrf_k = 17

        query = BM25HybridSearchAlgorithm(fusion="rrf")._build_fusion_query(settings)

        assert query.rrf.k == 17

    @pytest.mark.unit
    def test_dbsf_keeps_plain_fusion_query(self):
        # DBSF normalises score distributions rather than ranks, so it has no
        # ranking constant to set.
        settings = MagicMock()
        settings.vector_search_rrf_k = 60

        query = BM25HybridSearchAlgorithm(fusion="dbsf")._build_fusion_query(settings)

        assert isinstance(query, models.FusionQuery)
        assert query.fusion == models.Fusion.DBSF


class TestGranularity:
    """`granularity="document"` routes through Qdrant's native grouping.

    The point of the mode is that one document occupies one result row instead
    of competing chunk-by-chunk, so these assert the *query shape* sent to
    Qdrant — group_by/group_size/prefetch depth — not just that a call happened.
    """

    @pytest.mark.unit
    async def test_chunk_granularity_is_the_default_and_ungrouped(
        self, patched_search, monkeypatch
    ):
        qdrant = MagicMock()
        empty = MagicMock()
        empty.points = []
        qdrant.query_points = AsyncMock(return_value=empty)
        qdrant.query_points_groups = AsyncMock()
        monkeypatch.setattr(
            "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
            AsyncMock(return_value=qdrant),
        )

        await BM25HybridSearchAlgorithm().search(query="hello", user_id="alice")

        qdrant.query_points.assert_awaited_once()
        qdrant.query_points_groups.assert_not_awaited()

    @pytest.mark.unit
    async def test_document_granularity_groups_by_doc_id(
        self, patched_search, monkeypatch
    ):
        qdrant = MagicMock()
        grouped = MagicMock()
        grouped.groups = []
        qdrant.query_points = AsyncMock()
        qdrant.query_points_groups = AsyncMock(return_value=grouped)
        monkeypatch.setattr(
            "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
            AsyncMock(return_value=qdrant),
        )

        await BM25HybridSearchAlgorithm().search(
            query="hello", user_id="alice", limit=10, granularity="document"
        )

        qdrant.query_points.assert_not_awaited()
        kwargs = qdrant.query_points_groups.await_args.kwargs
        assert kwargs["group_by"] == "doc_id"
        # One row per document — extra hits would reintroduce the very
        # concentration this mode removes.
        assert kwargs["group_size"] == 1
        # limit counts groups (documents), carrying the same 2x over-fetch.
        assert kwargs["limit"] == 20
        # Fusion is unchanged: still the explicit-k RRF query.
        assert isinstance(kwargs["query"], models.RrfQuery)

    @pytest.mark.unit
    async def test_document_granularity_deepens_the_prefetch(
        self, patched_search, monkeypatch
    ):
        """Filling N distinct documents needs more candidates than N chunks."""
        qdrant = MagicMock()
        grouped = MagicMock()
        grouped.groups = []
        qdrant.query_points_groups = AsyncMock(return_value=grouped)
        monkeypatch.setattr(
            "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
            AsyncMock(return_value=qdrant),
        )

        await BM25HybridSearchAlgorithm().search(
            query="hello", user_id="alice", limit=10, granularity="document"
        )

        prefetch = qdrant.query_points_groups.await_args.kwargs["prefetch"]
        assert len(prefetch) == 2, "still dense + sparse"
        # 10 * 2 (over-fetch) * DOCUMENT_PREFETCH_FACTOR
        assert [p.limit for p in prefetch] == [80, 80]

    @pytest.mark.unit
    async def test_document_prefetch_is_capped(self, patched_search, monkeypatch):
        """The multipliers compound (tool 2x * dedup 2x * factor 4x), so an
        uncapped limit=100 would ask Qdrant for 1600 candidates per branch and
        measured ~10x the latency for identical results. Cap it."""
        qdrant = MagicMock()
        grouped = MagicMock()
        grouped.groups = []
        qdrant.query_points_groups = AsyncMock(return_value=grouped)
        monkeypatch.setattr(
            "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
            AsyncMock(return_value=qdrant),
        )

        # The tool layer passes limit*2, so limit=200 is the real high-end call.
        await BM25HybridSearchAlgorithm().search(
            query="hello", user_id="alice", limit=200, granularity="document"
        )

        prefetch = qdrant.query_points_groups.await_args.kwargs["prefetch"]
        uncapped = 200 * 2 * DOCUMENT_PREFETCH_FACTOR
        assert uncapped > MAX_DOCUMENT_PREFETCH, "fixture no longer exercises the cap"
        assert [p.limit for p in prefetch] == [
            MAX_DOCUMENT_PREFETCH,
            MAX_DOCUMENT_PREFETCH,
        ]

    @pytest.mark.unit
    async def test_groups_are_flattened_to_best_chunk_per_document(
        self, patched_search, monkeypatch
    ):
        """Each group collapses to its top hit, preserving the flat result shape
        every downstream stage (dedup, verification, context) already expects."""
        best = MagicMock(score=0.9)
        worse = MagicMock(score=0.1)
        group = MagicMock()
        group.hits = [best, worse]
        empty_group = MagicMock()
        empty_group.hits = []

        grouped = MagicMock()
        grouped.groups = [group, empty_group]
        qdrant = MagicMock()
        qdrant.query_points_groups = AsyncMock(return_value=grouped)
        monkeypatch.setattr(
            "nextcloud_mcp_server.search.bm25_hybrid.get_qdrant_client",
            AsyncMock(return_value=qdrant),
        )

        captured = []

        def fake_build(point, metadata_extras):
            captured.append(point)
            return None  # skip SearchResult construction; we only assert routing

        monkeypatch.setattr(
            "nextcloud_mcp_server.search.bm25_hybrid.build_search_result_from_point",
            fake_build,
        )

        await BM25HybridSearchAlgorithm().search(
            query="hello", user_id="alice", granularity="document"
        )

        # Only the best hit of the non-empty group; the empty group is skipped
        # rather than raising an IndexError.
        assert captured == [best]

    @pytest.mark.unit
    async def test_invalid_granularity_raises(self, patched_search):
        """Fails loudly rather than silently degrading to chunk granularity."""
        # Constructed outside the raises block so only one call inside it can
        # throw (python:S5778) — otherwise a constructor regression would pass
        # as if it were the validation firing.
        algo = BM25HybridSearchAlgorithm()

        with pytest.raises(ValueError, match="Invalid granularity"):
            await algo.search(query="hello", user_id="alice", granularity="documnet")
