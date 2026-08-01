"""Unit tests for the gateway rerank sub-client.

The response-parsing tests carry most of the weight here. A reranker that
silently drops a candidate looks exactly like a ranking change from the outside
while actually being lost recall, so the client's contract is: every index it
returns is valid and unique, and it never invents or omits one quietly.
"""

import httpx
import pytest

from nextcloud_mcp_server.providers.gateway_rerank import (
    GatewayRerankClient,
    RerankError,
)

pytestmark = pytest.mark.unit


def _patch_transport(monkeypatch, handler):
    """Wrap httpx.AsyncClient with a MockTransport, capturing issued requests.

    Mirrors tests/unit/providers/test_gateway_batch.py — this repo has no respx.
    """
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    original = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return seen


def _ok(payload):
    return lambda request: httpx.Response(200, json=payload)


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://gw.example", "https://gw.example/v1"),
        ("https://gw.example/", "https://gw.example/v1"),
        ("https://gw.example/v1", "https://gw.example/v1"),
        ("https://gw.example/v1/", "https://gw.example/v1"),
    ],
)
def test_base_url_normalization_is_idempotent(base, expected):
    """EMBEDDING_GATEWAY_URL is a bare origin in some deployments and already
    /v1-suffixed in others; both must reach the same endpoint."""
    assert GatewayRerankClient(base, "m")._base == expected


async def test_posts_model_query_and_documents(monkeypatch):
    seen = _patch_transport(
        monkeypatch, _ok({"results": [{"index": 0, "relevance_score": 0.5}]})
    )
    client = GatewayRerankClient("https://gw.example", "vendor/model")

    await client.rerank("who signed in", ["a", "b"])

    assert len(seen) == 1
    assert str(seen[0].url) == "https://gw.example/v1/rerank"
    import json

    body = json.loads(seen[0].content)
    assert body["model"] == "vendor/model"
    assert body["query"] == "who signed in"
    assert body["documents"] == ["a", "b"]
    # A provider-side top_n would drop the tail the caller still needs to
    # re-append in retrieval order.
    assert body["top_n"] == 2


async def test_no_auth_header_without_token_provider(monkeypatch):
    seen = _patch_transport(
        monkeypatch, _ok({"results": [{"index": 0, "relevance_score": 1.0}]})
    )
    await GatewayRerankClient("https://gw.example", "m").rerank("q", ["a", "b"])
    assert "authorization" not in {k.lower() for k in seen[0].headers}


async def test_bearer_header_from_token_provider(monkeypatch, mocker):
    seen = _patch_transport(
        monkeypatch, _ok({"results": [{"index": 0, "relevance_score": 1.0}]})
    )
    token_provider = mocker.MagicMock()
    token_provider.get_token = mocker.AsyncMock(return_value="tok-123")

    await GatewayRerankClient("https://gw.example", "m", token_provider).rerank(
        "q", ["a", "b"]
    )

    assert seen[0].headers["authorization"] == "Bearer tok-123"


async def test_documents_and_query_are_truncated(monkeypatch):
    """`document_chunk_size` is operator-configurable with no upper bound, so an
    untruncated pool is an unbounded request body — which fails as a 413 at the
    ingress, not as anything the gateway reports."""
    seen = _patch_transport(
        monkeypatch, _ok({"results": [{"index": 0, "relevance_score": 1.0}]})
    )
    await GatewayRerankClient("https://gw.example", "m").rerank(
        "q" * 50_000, ["d" * 50_000, "e" * 50_000]
    )

    import json

    body = json.loads(seen[0].content)
    assert len(body["query"]) < 5_000
    assert all(len(d) < 5_000 for d in body["documents"])


async def test_empty_document_list_short_circuits(monkeypatch):
    seen = _patch_transport(monkeypatch, _ok({"results": []}))
    assert await GatewayRerankClient("https://gw.example", "m").rerank("q", []) == []
    assert seen == []  # no request issued


async def test_non_2xx_raises_rerank_error(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(503, text="unavailable"))
    with pytest.raises(RerankError, match="503"):
        await GatewayRerankClient("https://gw.example", "m").rerank("q", ["a", "b"])


async def test_transport_failure_raises_rerank_error(monkeypatch):
    def _boom(request):
        raise httpx.ConnectError("no route to host")

    _patch_transport(monkeypatch, _boom)
    with pytest.raises(RerankError):
        await GatewayRerankClient("https://gw.example", "m").rerank("q", ["a", "b"])


class TestResponseParsing:
    """`_parse` is the contract boundary — everything downstream trusts it."""

    def test_orders_by_response_order_not_index(self):
        ranked = GatewayRerankClient._parse(
            {
                "results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                ]
            },
            3,
        )
        assert [r.index for r in ranked] == [2, 0]
        assert ranked[0].score == 0.9

    def test_out_of_range_indices_are_discarded(self):
        """A provider bug must not index past the caller's list."""
        ranked = GatewayRerankClient._parse(
            {
                "results": [
                    {"index": 99, "relevance_score": 1.0},
                    {"index": -1, "relevance_score": 1.0},
                    {"index": 1, "relevance_score": 0.5},
                ]
            },
            2,
        )
        assert [r.index for r in ranked] == [1]

    def test_duplicate_indices_are_discarded(self):
        """Otherwise one candidate would occupy two result slots."""
        ranked = GatewayRerankClient._parse(
            {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
            2,
        )
        assert [r.index for r in ranked] == [0]

    def test_partial_results_are_allowed(self):
        """Providers may cap results. The client returns what it can verify and
        leaves re-appending the remainder to the caller — never dropping."""
        ranked = GatewayRerankClient._parse(
            {"results": [{"index": 3, "relevance_score": 0.9}]}, 10
        )
        assert [r.index for r in ranked] == [3]

    def test_negative_scores_are_preserved(self):
        """Raw cross-encoder logits can be negative. Clamping here would collapse
        a negative tail into a tie and silently leave it in retrieval order."""
        ranked = GatewayRerankClient._parse(
            {
                "results": [
                    {"index": 0, "relevance_score": -0.2},
                    {"index": 1, "relevance_score": -5.0},
                ]
            },
            2,
        )
        assert [r.score for r in ranked] == [-0.2, -5.0]

    @pytest.mark.parametrize(
        "item",
        [
            {"index": True, "relevance_score": 1.0},  # bool is an int subclass
            {"index": "0", "relevance_score": 1.0},
            {"index": 0, "relevance_score": "high"},
            {"index": 0, "relevance_score": True},
            {"index": 0},
            "not-a-dict",
        ],
    )
    def test_malformed_entries_are_skipped(self, item):
        with pytest.raises(RerankError, match="no usable results"):
            GatewayRerankClient._parse({"results": [item]}, 2)

    @pytest.mark.parametrize(
        "body",
        [
            {"results": "nope"},
            {},
            [],
            "not-an-object",
        ],
    )
    def test_unusable_bodies_raise(self, body):
        with pytest.raises(RerankError):
            GatewayRerankClient._parse(body, 2)
