"""Unit tests for the search-path usage-metering helper (Deck #67).

``record_search_usage`` records the billable ``tokens_embedded`` event for a
semantic search. These pin the value mapping (query token count), the flag-off
no-op, the doc_types metadata bounding, and the best-effort failure path —
covering the metering wiring without standing up the full ``nc_semantic_search``
tool.

The helper lives in ``nextcloud_mcp_server.usage.search`` because BOTH search
entrypoints record through it (``nc_semantic_search`` and ``POST
/api/v1/search``), and ``api/`` must not import from ``server/``. It is still
re-exported as ``server.semantic.record_search_usage``, which is what these
tests call — the monkeypatch target, however, must be the defining module.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.providers.base import ByokUsage
from nextcloud_mcp_server.server import semantic
from nextcloud_mcp_server.usage import search as usage_search


@pytest.fixture
def store_spy(monkeypatch):
    """Patch UsageEventStore.shared() to return a spy store."""
    store = MagicMock()
    store.record_usage_events = AsyncMock()
    monkeypatch.setattr(
        usage_search.UsageEventStore, "shared", AsyncMock(return_value=store)
    )
    return store


def _events(store_spy):
    """The ``UsageEvent`` list from the single record_usage_events() call."""
    store_spy.record_usage_events.assert_awaited_once()
    return list(store_spy.record_usage_events.await_args.args[0])


def _event(store_spy):
    """The one event of a platform-key search."""
    (event,) = _events(store_spy)
    return event


@pytest.mark.unit
async def test_records_query_token_count(store_spy):
    """The event value is the query embedding's token count."""
    await semantic.record_search_usage(
        enabled=True,
        user_id="alice",
        fusion="rrf",
        doc_types=["note", "file"],
        token_count=42,
    )

    event = _event(store_spy)
    assert event.metric == "tokens_embedded"
    assert event.value == 42
    assert store_spy.record_usage_events.await_args.kwargs["enabled"] is True
    assert event.metadata["user_id"] == "alice"
    assert event.metadata["fusion"] == "rrf"
    assert event.metadata["doc_types"] == ["note", "file"]


@pytest.mark.unit
async def test_disabled_is_noop(store_spy):
    """Flag off → no store access, no event."""
    await semantic.record_search_usage(
        enabled=False,
        user_id="alice",
        fusion="rrf",
        doc_types=None,
        token_count=10,
    )
    store_spy.record_usage_events.assert_not_awaited()


@pytest.mark.unit
async def test_none_token_count_records_zero(store_spy):
    """A missing token count (pre-embed error) records value 0, not None."""
    await semantic.record_search_usage(
        enabled=True,
        user_id="alice",
        fusion="dbsf",
        doc_types=None,
        token_count=None,
    )
    event = _event(store_spy)
    assert event.value == 0
    # None and [] both normalize to null for consistent IS NULL counting.
    assert event.metadata["doc_types"] is None


@pytest.mark.unit
async def test_empty_doc_types_normalizes_to_null(store_spy):
    """An empty doc_types list normalizes to None, same as a None input, so a
    metadata->'doc_types' IS NULL query counts the all-types case consistently."""
    await semantic.record_search_usage(
        enabled=True,
        user_id="alice",
        fusion="rrf",
        doc_types=[],
        token_count=5,
    )
    assert _event(store_spy).metadata["doc_types"] is None


@pytest.mark.unit
async def test_doc_types_metadata_is_bounded(store_spy):
    """A large doc_types list is truncated to the metadata cap."""
    many = [f"type-{i}" for i in range(40)]
    await semantic.record_search_usage(
        enabled=True,
        user_id="alice",
        fusion="rrf",
        doc_types=many,
        token_count=5,
    )
    recorded = _event(store_spy).metadata["doc_types"]
    assert recorded == many[: usage_search._USAGE_METADATA_MAX_DOC_TYPES]


@pytest.mark.unit
async def test_byok_query_records_byok_metrics_not_tokens_embedded(store_spy):
    """A query the gateway embedded on the tenant's own key is metered as the
    display-only BYOK metrics, never as billed tokens_embedded."""
    await semantic.record_search_usage(
        enabled=True,
        user_id="alice",
        fusion="rrf",
        doc_types=None,
        token_count=9,
        byok=ByokUsage(tokens=9, requests=1),
    )
    by_metric = {e.metric: e.value for e in _events(store_spy)}
    assert by_metric == {"tokens_embedded_byok": 9, "embedding_requests_byok": 1}


@pytest.mark.unit
async def test_store_failure_is_swallowed(monkeypatch):
    """A store-construction failure is logged, never raised into the search."""
    monkeypatch.setattr(
        usage_search.UsageEventStore,
        "shared",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    # Must not raise.
    await semantic.record_search_usage(
        enabled=True,
        user_id="alice",
        fusion="rrf",
        doc_types=None,
        token_count=7,
    )
