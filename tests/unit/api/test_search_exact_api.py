"""``algorithm="exact"`` on POST /api/v1/search: the real handler, pinning
the contract Astrolabe pages through (true total, stable non-overlapping
pages, nothing cut by the ranked paths' candidate budgets)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import unified_search
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.exact import ExactMatchAlgorithm
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit


def _docs(n: int, doc_type: str = "file") -> list[SearchResult]:
    return [
        SearchResult(
            id=str(i), doc_type=doc_type, title=f"d{i:04}", excerpt="x", score=1.0
        )
        for i in range(n)
    ]


def _post(body: dict, search: AsyncMock, *, truncated: bool = False):
    settings = MagicMock()
    settings.vector_sync_enabled = True
    app = Starlette(routes=[Route("/api/v1/search", unified_search, methods=["POST"])])
    app.state.oauth_context = {"config": {"nextcloud_host": "https://nc.example"}}

    async def run(self, *args, **kwargs):
        self.truncated = truncated
        return await search(*args, **kwargs)

    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings", return_value=settings
        ),
        patch(
            "nextcloud_mcp_server.api.management.get_settings", return_value=settings
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
            new=AsyncMock(side_effect=NotProvisionedError("not provisioned")),
        ),
        patch.object(ExactMatchAlgorithm, "search", run),
    ):
        return TestClient(app).post(
            "/api/v1/search", json={"algorithm": "exact", **body}
        )


def test_total_is_every_match_and_pages_do_not_overlap():
    search = AsyncMock(return_value=_docs(250))
    pages = [
        _post({"query": "Jane Doe", "limit": 100, "offset": o}, search).json()
        for o in (0, 100, 200)
    ]

    assert [p["total_found"] for p in pages] == [250, 250, 250]
    ids = [r["id"] for p in pages for r in p["results"]]
    assert len(ids) == 250 and len(set(ids)) == 250
    assert pages[0]["granularity"] == "document"
    assert pages[0]["truncated"] is False
    assert {r["relevance_source"] for r in pages[0]["results"]} == {"exact_match"}


def test_several_doc_types_are_not_cut_to_a_candidate_budget():
    # The ranked doc_types loop caps the merged list at 2 x (limit + offset).
    search = AsyncMock(side_effect=lambda **kw: _docs(300, kw["doc_type"]))
    body = _post(
        {"query": "Jane Doe", "limit": 10, "doc_types": ["file", "note"]}, search
    ).json()

    assert body["total_found"] == 600


def test_truncation_is_reported():
    body = _post(
        {"query": "Jane Doe"}, AsyncMock(return_value=_docs(3)), truncated=True
    )

    assert body.json()["truncated"] is True


def test_min_relevance_keeps_exact_matches():
    body = _post(
        {"query": "Jane Doe", "min_relevance": 0.99}, AsyncMock(return_value=_docs(5))
    )

    assert body.json()["total_found"] == 5


def test_rerank_is_rejected():
    resp = _post({"query": "Jane Doe", "rerank": True}, AsyncMock(return_value=[]))

    assert resp.status_code == 400
    assert "exact" in resp.json()["error"]
