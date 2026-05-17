"""Unit tests for the embedding singleton accessors.

Pins the invariant that ``get_bm25_service()`` instantiates its
singleton OFF the event loop. The BM25 provider's constructor calls
``fastembed.SparseTextEmbedding(model_name=...)`` which downloads
~50 MB of model weights from HuggingFace and loads them into memory
— observed >5 s wall-clock in production. If a future refactor
inlines the construction back into the coroutine, kubernetes
``/health/live`` httpGet probes will start timing out and pods will
crashloop (regression seen in the Astrolabe Cloud deck #102 smoke
that prompted this fix).
"""

from __future__ import annotations

import time

import anyio
import pytest

from nextcloud_mcp_server.embedding import service as svc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the singleton so each test sees a cold path."""
    monkeypatch.setattr(svc, "_bm25_service", None)


async def test_get_bm25_service_runs_init_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow synchronous BM25 init must NOT block other coroutines.

    Simulate the FastEmbed model download with a 1 s blocking sleep
    inside the provider's ``__init__``. While that init is in flight,
    a concurrent ``anyio.sleep(0.05)`` must finish promptly — proving
    the init was offloaded to a worker thread rather than blocking the
    event loop.

    Without the ``anyio.to_thread.run_sync`` wrapper this test would
    fail: the concurrent sleep would not be scheduled until after the
    blocking constructor returned.
    """
    init_block_seconds = 1.0
    concurrent_sleep_seconds = 0.05

    class _BlockingFake:
        def __init__(self) -> None:
            # Stand-in for SparseTextEmbedding's slow constructor.
            time.sleep(init_block_seconds)

    monkeypatch.setattr(svc, "BM25SparseEmbeddingProvider", _BlockingFake)

    concurrent_elapsed: float | None = None

    async def _other_work() -> None:
        nonlocal concurrent_elapsed
        started = time.monotonic()
        await anyio.sleep(concurrent_sleep_seconds)
        concurrent_elapsed = time.monotonic() - started

    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(svc.get_bm25_service)
        tg.start_soon(_other_work)
    total_elapsed = time.monotonic() - started

    assert concurrent_elapsed is not None
    # The concurrent task should finish in roughly the time of its own
    # anyio.sleep — not delayed by the 1 s constructor. Generous bound
    # for flakiness; the meaningful contrast is "well under 1 s".
    assert concurrent_elapsed < 0.5, (
        f"concurrent anyio.sleep took {concurrent_elapsed:.3f}s — "
        "event loop was blocked by BM25SparseEmbeddingProvider init "
        "(regression: missing anyio.to_thread.run_sync wrapper)"
    )
    # Total wall-clock should be roughly the init time (the longer of
    # the two), confirming the tasks ran in parallel.
    assert total_elapsed >= init_block_seconds * 0.9
    assert total_elapsed < init_block_seconds + 0.5


async def test_get_bm25_service_caches_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call must return the same instance without re-running
    the constructor — the singleton contract is the whole reason
    ``get_bm25_service`` exists."""
    call_count = 0

    class _CountingFake:
        def __init__(self) -> None:
            nonlocal call_count
            call_count += 1

    monkeypatch.setattr(svc, "BM25SparseEmbeddingProvider", _CountingFake)

    first = await svc.get_bm25_service()
    second = await svc.get_bm25_service()

    assert first is second
    assert call_count == 1, (
        f"BM25SparseEmbeddingProvider was constructed {call_count} times — "
        "the singleton accessor should construct exactly once"
    )
