"""Unit tests for the Astrolabe searchable-sources capability reader."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import nextcloud_mcp_server.capabilities as cap
from nextcloud_mcp_server.capabilities import (
    _parse_enabled_doc_types,
    allowed_doc_types,
    clear_cache,
    is_doc_type_allowed,
)

pytestmark = pytest.mark.unit


def _payload(enabled_doc_types) -> dict:
    """Build an OCS capabilities envelope carrying the astrolabe block.

    ``enabled_doc_types=...`` (Ellipsis) omits the key entirely.
    """
    semantic: dict = {}
    if enabled_doc_types is not ...:
        semantic["enabled_doc_types"] = enabled_doc_types
    return {
        "ocs": {
            "meta": {"status": "ok"},
            "data": {"capabilities": {"astrolabe": {"semantic_search": semantic}}},
        }
    }


# ---------------------------------------------------------------------------
# _parse_enabled_doc_types
# ---------------------------------------------------------------------------


def test_parse_present_list_returns_set():
    assert _parse_enabled_doc_types(_payload(["note", "file"])) == {"note", "file"}


def test_parse_empty_list_returns_empty_set():
    # Admin disabled every source — distinct from "no restriction".
    assert _parse_enabled_doc_types(_payload([])) == set()


def test_parse_missing_astrolabe_block_returns_none():
    payload = {"ocs": {"data": {"capabilities": {}}}}
    assert _parse_enabled_doc_types(payload) is None


def test_parse_missing_enabled_key_returns_none():
    assert _parse_enabled_doc_types(_payload(...)) is None


def test_parse_malformed_payload_returns_none():
    assert _parse_enabled_doc_types(None) is None
    assert _parse_enabled_doc_types({"ocs": "nope"}) is None
    assert _parse_enabled_doc_types(_payload("not-a-list")) is None


def test_parse_drops_non_string_entries():
    assert _parse_enabled_doc_types(_payload(["note", 5, None])) == {"note"}


# ---------------------------------------------------------------------------
# is_doc_type_allowed
# ---------------------------------------------------------------------------


def test_is_doc_type_allowed_none_means_no_restriction():
    assert is_doc_type_allowed("anything", None) is True


def test_is_doc_type_allowed_respects_set():
    allowed = frozenset({"note"})
    assert is_doc_type_allowed("note", allowed) is True
    assert is_doc_type_allowed("file", allowed) is False


def test_is_doc_type_allowed_empty_set_blocks_all():
    assert is_doc_type_allowed("note", frozenset()) is False


# ---------------------------------------------------------------------------
# allowed_doc_types (cache + fail-open)
# ---------------------------------------------------------------------------


def _client(payload=None, raises: Exception | None = None) -> AsyncMock:
    """An object with an async ``capabilities()`` method (AsyncMock-backed)."""
    m = AsyncMock()
    if raises is not None:
        m.capabilities.side_effect = raises
    else:
        m.capabilities.return_value = payload
    return m


async def test_allowed_doc_types_parses_and_caches():
    clear_cache()
    client = _client(_payload(["note", "file"]))

    first = await allowed_doc_types(client, "alice")
    second = await allowed_doc_types(client, "alice")

    assert first == frozenset({"note", "file"})
    assert second == frozenset({"note", "file"})
    # Second call served from the cache — only one OCS round-trip.
    assert client.capabilities.await_count == 1


async def test_allowed_doc_types_missing_block_returns_none():
    clear_cache()
    client = _client({"ocs": {"data": {"capabilities": {}}}})
    assert await allowed_doc_types(client, "bob") is None


async def test_allowed_doc_types_fail_open_not_cached():
    clear_cache()
    client = _client(raises=RuntimeError("ocs down"))

    assert await allowed_doc_types(client, "carol") is None
    # Failures are not cached — the next call retries the OCS lookup.
    assert await allowed_doc_types(client, "carol") is None
    assert client.capabilities.await_count == 2


async def test_allowed_doc_types_cache_is_per_user():
    clear_cache()
    alice = _client(_payload(["note"]))
    bob = _client(_payload(["file"]))

    assert await allowed_doc_types(alice, "alice") == frozenset({"note"})
    assert await allowed_doc_types(bob, "bob") == frozenset({"file"})


async def test_allowed_doc_types_refetches_after_ttl(monkeypatch):
    clear_cache()
    client = _client(_payload(["note"]))

    # Drive the module clock so the second call lands past the TTL window.
    clock = {"now": 1000.0}
    monkeypatch.setattr(cap.time, "monotonic", lambda: clock["now"])

    await allowed_doc_types(client, "erin")
    clock["now"] += cap._CACHE_TTL_SECONDS + 1
    await allowed_doc_types(client, "erin")

    assert client.capabilities.await_count == 2


async def test_clear_cache_forces_refetch():
    clear_cache()
    client = _client(_payload(["note"]))
    await allowed_doc_types(client, "dave")
    cap.clear_cache()
    await allowed_doc_types(client, "dave")
    assert client.capabilities.await_count == 2
