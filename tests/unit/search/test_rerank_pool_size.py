"""`effective_pool_size` — the one piece of rerank machinery with a non-obvious
contract, so it is tested here with the machinery rather than with the wiring.

Two constraints govern the pool depth and they can conflict: never retrieve less
than the surface would have retrieved anyway, and cap grouped retrieval at what
the grouped prefetch can actually fill. Which one wins is a judgement call, and
these tests pin it in both directions so the resolution is a decision on record
rather than an artifact of expression order.
"""

import pytest

from nextcloud_mcp_server.search import rerank as rerank_mod
from nextcloud_mcp_server.search.bm25_hybrid import (
    DOCUMENT_PREFETCH_FACTOR,
    MAX_DOCUMENT_PREFETCH,
)

pytestmark = pytest.mark.unit

_GROUPED_CAP = MAX_DOCUMENT_PREFETCH // DOCUMENT_PREFETCH_FACTOR


def _settings(**overrides):
    class _S:
        search_rerank_enabled = True
        embedding_gateway_url = "https://gw.example"
        search_rerank_model = "vendor/model"
        search_rerank_pool_size = 200
        search_rerank_timeout_seconds = 30.0
        search_rerank_max_concurrency = 1

    s = _S()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def test_uses_the_configured_pool_when_deeper_than_the_floor():
    assert (
        rerank_mod.effective_pool_size(
            _settings(search_rerank_pool_size=200), floor=20, grouped=False
        )
        == 200
    )


def test_never_below_the_floor():
    """Reranking must not cause a request to retrieve fewer candidates than the
    same request without it — that would drop rows a caller already received."""
    assert (
        rerank_mod.effective_pool_size(
            _settings(search_rerank_pool_size=50), floor=400, grouped=False
        )
        == 400
    )


def test_ungrouped_search_is_not_capped():
    """The cap is a property of grouped retrieval; chunk granularity has no
    equivalent ceiling, so a large configured pool must pass through."""
    assert (
        rerank_mod.effective_pool_size(
            _settings(search_rerank_pool_size=5000), floor=10, grouped=False
        )
        == 5000
    )


def test_grouped_search_is_capped_when_there_is_room_to_honour_it():
    """With the floor below the cap, the cap must actually bind — otherwise the
    clamp is dead code."""
    assert (
        rerank_mod.effective_pool_size(
            _settings(search_rerank_pool_size=_GROUPED_CAP * 10),
            floor=_GROUPED_CAP - 1,
            grouped=True,
        )
        == _GROUPED_CAP
    )


def test_grouped_cap_yields_to_the_floor_when_they_conflict():
    """When the floor alone already exceeds the cap, the FLOOR wins.

    Deliberate precedence, not the cap leaking. In that regime the *unreranked*
    path already requests `floor` groups and already pays the grouped
    degradation, so honouring the cap here would not avoid it — it would only
    return fewer rows than the same request with reranking off. Degraded
    ordering is recoverable by the reranker that follows; missing rows are not.
    """
    floor = _GROUPED_CAP * 5
    assert (
        rerank_mod.effective_pool_size(
            _settings(search_rerank_pool_size=_GROUPED_CAP * 10),
            floor=floor,
            grouped=True,
        )
        == floor
    )


@pytest.mark.parametrize("grouped", [True, False])
def test_result_is_never_below_the_floor_for_any_configured_size(grouped):
    """Property check across the interesting range: whatever the configuration
    and whichever constraint binds, the floor invariant must hold."""
    for configured in (1, 10, _GROUPED_CAP, _GROUPED_CAP * 3):
        for floor in (1, 50, _GROUPED_CAP, _GROUPED_CAP * 2):
            pool = rerank_mod.effective_pool_size(
                _settings(search_rerank_pool_size=configured),
                floor=floor,
                grouped=grouped,
            )
            assert pool >= floor
