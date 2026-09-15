"""`nc_semantic_search` records exactly one search metric per exit path.

The MCP tool records its request metric in a `finally`, which is what makes the
guarantee hold for success, `MCPError`, and unexpected raises alike. That is a
claim about control flow, so it is only worth anything if something exercises
each path — a `finally` that was accidentally moved inside the `try`, or an
early `return` added above it, would still pass every other test in the suite.

Mirrors the spy pattern in tests/unit/api/test_search_usage_metering.py so the
two entrypoints are held to the same standard.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.shared.exceptions import MCPError

pytestmark = pytest.mark.unit


def _build_tool():
    """Register the tools against a stub MCPServer and return `nc_semantic_search`."""
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


def _ctx():
    ctx = MagicMock()
    ctx.request_context.lifespan_context.eviction_task_group = None
    return ctx


def _settings(vector_sync=True):
    s = MagicMock()
    s.vector_sync_enabled = vector_sync
    s.usage_metering_enabled = False
    s.search_rerank_enabled = False
    s.embedding_gateway_url = ""
    return s


def _run(
    *,
    allowed=None,
    search_side_effect=None,
    search_return=None,
    vector_sync=True,
    **tool_kwargs,
):
    """Call the tool with everything below it stubbed; return the metric spy."""
    spy = MagicMock()
    algo = MagicMock()
    algo.search = (
        AsyncMock(side_effect=search_side_effect)
        if search_side_effect
        else AsyncMock(return_value=search_return or [])
    )
    algo.query_token_count = 0
    algo.query_embedding = None

    client = MagicMock()
    client.sharing = MagicMock()

    scope = MagicMock()
    scope.owners = ["alice"]
    scope.share_root_ids = []

    mod = "nextcloud_mcp_server.server.semantic"
    # `@require_scopes` consults its OWN module's settings, not the tool's. With
    # that left ambient the scope check followed whatever deployment mode the
    # environment implied — passing locally under BasicAuth-like config and
    # denying under login-flow config in CI. Pinning it here keeps these tests
    # about metrics rather than about which .env the runner happened to have.
    scope_settings = MagicMock()
    scope_settings.enable_login_flow = False
    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=scope_settings,
        ),
        patch(f"{mod}.get_settings", return_value=_settings(vector_sync)),
        patch(f"{mod}.get_client", new=AsyncMock(return_value=client)),
        patch(f"{mod}.list_accessible_scope", new=AsyncMock(return_value=scope)),
        patch(f"{mod}.allowed_doc_types", new=AsyncMock(return_value=allowed)),
        patch(f"{mod}.normalize_path_prefixes", return_value=[]),
        patch(f"{mod}.resolve_prefix_folder_ids", new=AsyncMock(return_value=[])),
        patch(f"{mod}.BM25HybridSearchAlgorithm", return_value=algo),
        patch(f"{mod}.verify_search_results", new=AsyncMock(return_value=([], 0))),
        patch(f"{mod}.record_search_usage", new=AsyncMock()),
        patch(f"{mod}.record_search_request", new=spy),
    ):
        tool = _build_tool()
        import anyio

        async def _go():
            return await tool(query="q", ctx=_ctx(), **tool_kwargs)

        try:
            result = anyio.run(_go)
        except MCPError as e:
            return spy, e
        return spy, result


def test_success_records_exactly_one_sample():
    spy, _ = _run()

    assert spy.call_count == 1
    kwargs = spy.call_args.kwargs
    assert kwargs["surface"] == "mcp"
    assert kwargs["status"] == "success"


def test_unexpected_exception_records_an_error_sample():
    """The `finally` is what makes this hold — a raise from deep inside the
    search must not simply vanish from the request counter."""
    spy, err = _run(search_side_effect=RuntimeError("qdrant exploded"))

    assert isinstance(err, MCPError)
    assert spy.call_count == 1
    assert spy.call_args.kwargs["status"] == "error"


def test_mcp_error_path_records_an_error_sample():
    """A client-facing MCPError is still a failed search."""
    spy, err = _run(search_side_effect=ValueError("No embedding provider configured"))

    assert isinstance(err, MCPError)
    assert spy.call_count == 1
    assert spy.call_args.kwargs["status"] == "error"


def test_consent_short_circuit_records_a_zero_result_success():
    """Not an error, and not nothing: recording it keeps the zero-result
    distribution honest about how often admin consent is the cause."""
    spy, _ = _run(allowed=frozenset(), doc_types=["file"])

    assert spy.call_count == 1
    kwargs = spy.call_args.kwargs
    assert kwargs["status"] == "success"
    assert kwargs["results_returned"] == 0


def test_granularity_is_reported_as_requested():
    spy, _ = _run(granularity="document")

    assert spy.call_args.kwargs["granularity"] == "document"


def test_reranked_label_is_false_when_not_requested():
    spy, _ = _run()

    assert spy.call_args.kwargs["reranked"] == "false"


def test_arbitrary_fusion_cannot_reach_the_metric_label():
    """`fusion` is caller-controlled and is not validated until the algorithm is
    constructed — which happens AFTER the label is computed, and inside the try
    whose `finally` records the sample regardless.

    So an unrecognised value must clamp rather than interpolate. Otherwise every
    distinct string a caller sends mints a permanent Prometheus series, which
    any holder of `semantic.read` could exploit without even completing a
    search.
    """
    spy, _ = _run(fusion="../../etc/passwd\n\nrandom-unique-value")

    assert spy.call_count == 1
    assert spy.call_args.kwargs["algorithm"] == "bm25_hybrid_rrf"

    # This harness stubs BM25HybridSearchAlgorithm, so it cannot show that the
    # bad mode still fails the request — asserting that here would only be
    # testing the mock. The real rejection is pinned one layer down, below.


def test_the_real_algorithm_still_rejects_an_invalid_fusion():
    """Bounding the LABEL must not make an invalid request succeed."""
    from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm

    with pytest.raises(ValueError, match="Invalid fusion"):
        BM25HybridSearchAlgorithm(fusion="not-a-mode")


def test_valid_fusion_is_reported_verbatim():
    """Clamping must not flatten the modes that genuinely differ."""
    spy, _ = _run(fusion="dbsf")

    assert spy.call_args.kwargs["algorithm"] == "bm25_hybrid_dbsf"
