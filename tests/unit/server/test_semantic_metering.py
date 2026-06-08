"""Unit tests for the search-path usage-metering helper (Deck #67).

``record_search_usage`` records the billable ``tokens_embedded`` event for a
semantic search. These pin the value mapping (query token count), the flag-off
no-op, the doc_types metadata bounding, and the best-effort failure path —
covering the server-tool metering wiring without standing up the full
``nc_semantic_search`` tool.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.server import semantic


@pytest.fixture
def store_spy(monkeypatch):
    """Patch UsageEventStore.shared() to return a spy store."""
    store = MagicMock()
    store.record_usage_event = AsyncMock()
    monkeypatch.setattr(
        semantic.UsageEventStore, "shared", AsyncMock(return_value=store)
    )
    return store


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

    store_spy.record_usage_event.assert_awaited_once()
    kwargs = store_spy.record_usage_event.await_args.kwargs
    assert kwargs["metric"] == "tokens_embedded"
    assert kwargs["value"] == 42
    assert kwargs["enabled"] is True
    assert kwargs["metadata"]["user_id"] == "alice"
    assert kwargs["metadata"]["fusion"] == "rrf"
    assert kwargs["metadata"]["doc_types"] == ["note", "file"]


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
    store_spy.record_usage_event.assert_not_awaited()


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
    kwargs = store_spy.record_usage_event.await_args.kwargs
    assert kwargs["value"] == 0
    # None and [] both normalize to null for consistent IS NULL counting.
    assert kwargs["metadata"]["doc_types"] is None


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
    kwargs = store_spy.record_usage_event.await_args.kwargs
    assert kwargs["metadata"]["doc_types"] is None


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
    recorded = store_spy.record_usage_event.await_args.kwargs["metadata"]["doc_types"]
    assert recorded == many[: semantic._USAGE_METADATA_MAX_DOC_TYPES]


@pytest.mark.unit
async def test_store_failure_is_swallowed(monkeypatch):
    """A store-construction failure is logged, never raised into the search."""
    monkeypatch.setattr(
        semantic.UsageEventStore,
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
