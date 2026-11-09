"""``nc_semantic_search(algorithm=...)``: hybrid (default), semantic, bm25 and
exact through the real tool, with Nextcloud and Qdrant stubbed."""

from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm
from nextcloud_mcp_server.search.exact import ExactMatchAlgorithm
from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm

pytestmark = pytest.mark.unit

_MOD = "nextcloud_mcp_server.server.semantic"


def _docs(n: int, doc_type: str = "file") -> list[SearchResult]:
    return [
        SearchResult(
            id=str(i), doc_type=doc_type, title=f"d{i:04}", excerpt="x", score=1.0
        )
        for i in range(n)
    ]


def _tool():
    from nextcloud_mcp_server.server.semantic import configure_semantic_tools

    captured = {}

    class _Mcp:
        def tool(self, **kwargs):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    configure_semantic_tools(_Mcp())
    return captured["nc_semantic_search"]


def _call(search: AsyncMock, *, truncated: bool = False, **kwargs):
    """Run the tool with every algorithm's ``search`` replaced by ``search``.

    Returns ``(response or MCPError, the algorithm instance that ran)``.
    """
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.usage_metering_enabled = False
    scope_settings = MagicMock()
    scope_settings.enable_login_flow = False
    scope = MagicMock(owners=["alice"], share_root_ids=[])
    ran: list = []

    async def run(self, *args, **kw):
        ran.append(self)
        if isinstance(self, ExactMatchAlgorithm):
            self.truncated = truncated
        return await search(*args, **kw)

    async def verify(_client, results, **_kw):
        return results, 0

    ctx = MagicMock()
    ctx.request_context.lifespan_context.eviction_task_group = None
    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=scope_settings,
        ),
        patch(f"{_MOD}.get_settings", return_value=settings),
        patch(f"{_MOD}.get_client", new=AsyncMock(return_value=MagicMock())),
        patch(f"{_MOD}.list_accessible_scope", new=AsyncMock(return_value=scope)),
        patch(f"{_MOD}.allowed_doc_types", new=AsyncMock(return_value=None)),
        patch(f"{_MOD}.verify_search_results", new=verify),
        patch(f"{_MOD}.record_search_usage", new=AsyncMock()),
        patch(f"{_MOD}.astrolabe_browser_base", return_value=None),
        patch.object(ExactMatchAlgorithm, "search", run),
        patch.object(BM25HybridSearchAlgorithm, "search", run),
        patch.object(SemanticSearchAlgorithm, "search", run),
    ):
        tool = _tool()

        async def go():
            return await tool(query="Jane Doe", ctx=ctx, **kwargs)

        try:
            return anyio.run(go), ran[0] if ran else None
        except MCPError as e:
            return e, ran[0] if ran else None


def test_hybrid_is_the_default():
    resp, algo = _call(AsyncMock(return_value=_docs(3)))

    assert isinstance(algo, BM25HybridSearchAlgorithm) and algo.dense
    assert resp.search_method == "bm25_hybrid_rrf"
    assert resp.truncated is None


@pytest.mark.parametrize(
    ("algorithm", "cls", "dense", "label"),
    [
        ("semantic", SemanticSearchAlgorithm, None, "semantic"),
        ("bm25", BM25HybridSearchAlgorithm, False, "bm25"),
    ],
)
def test_ranked_algorithms_are_selectable(algorithm, cls, dense, label):
    resp, algo = _call(AsyncMock(return_value=_docs(3)), algorithm=algorithm)

    assert type(algo) is cls
    if dense is not None:
        assert algo.dense is dense
    assert resp.search_method == label
    assert len(resp.results) == 3


def test_exact_returns_the_true_total_in_stable_non_overlapping_pages():
    search = AsyncMock(return_value=_docs(250))
    pages = [
        _call(search, algorithm="exact", limit=100, offset=o)[0] for o in (0, 100, 200)
    ]

    assert [p.total_found for p in pages] == [250, 250, 250]
    ids = [r.id for p in pages for r in p.results]
    assert len(ids) == 250 and len(set(ids)) == 250
    assert pages[0].search_method == "exact"
    assert pages[0].granularity == "document"
    assert pages[0].truncated is False
    assert {r.relevance for r in pages[0].results} == {1.0}


def test_exact_across_doc_types_is_not_cut_to_a_candidate_budget():
    search = AsyncMock(side_effect=lambda **kw: _docs(300, kw["doc_type"]))
    resp, _ = _call(search, algorithm="exact", limit=10, doc_types=["file", "note"])

    assert resp.total_found == 600


def test_exact_reports_truncation():
    resp, _ = _call(AsyncMock(return_value=_docs(5)), algorithm="exact", truncated=True)

    assert resp.truncated is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"algorithm": "exact", "rerank": True}, "rerank does not apply"),
        ({"offset": 10}, "offset is supported only"),
        ({"algorithm": "bm25", "offset": 10}, "offset is supported only"),
        (
            {"algorithm": "semantic", "granularity": "document"},
            "granularity='document'",
        ),
        ({"algorithm": "exact", "score_threshold": 0.5}, "score_threshold"),
    ],
)
def test_option_combinations_an_algorithm_cannot_honour_are_rejected(kwargs, message):
    search = AsyncMock(return_value=_docs(1))
    err, _ = _call(search, **kwargs)

    assert isinstance(err, MCPError)
    assert message in str(err)
    search.assert_not_awaited()


@pytest.mark.parametrize("algorithm", ["hybrid", "semantic", "bm25", "exact"])
def test_an_invalid_fusion_is_rejected_for_every_algorithm(algorithm):
    search = AsyncMock(return_value=[])
    err, _ = _call(search, algorithm=algorithm, fusion="not-a-mode")

    assert isinstance(err, MCPError)
    assert "Invalid fusion" in str(err)
    search.assert_not_awaited()
