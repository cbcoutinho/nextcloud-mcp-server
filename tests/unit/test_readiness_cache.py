"""Unit tests for the readiness dependency-health cache (Deck #302).

The readiness probe reads this snapshot without performing any I/O; these tests
pin the small amount of logic it relies on (update/snapshot/staleness).
"""

import pytest

from nextcloud_mcp_server.observability.readiness import (
    DependencyStatus,
    ReadinessCache,
)

pytestmark = pytest.mark.unit


def test_dependency_status_defaults():
    status = DependencyStatus(name="nextcloud")
    assert status.healthy is None  # not yet checked
    assert status.detail == "pending"
    assert status.checked_at == pytest.approx(0.0)


def test_update_and_snapshot_round_trip():
    cache = ReadinessCache()
    cache.update("nextcloud_reachable", True, "ok", now=100.0)
    cache.update("qdrant", False, "error: status 503", now=100.0)

    snap = cache.snapshot()
    assert snap["nextcloud_reachable"].healthy is True
    assert snap["nextcloud_reachable"].detail == "ok"
    assert snap["qdrant"].healthy is False
    assert snap["qdrant"].detail == "error: status 503"


def test_snapshot_is_a_copy():
    cache = ReadinessCache()
    cache.update("nextcloud_reachable", True, "ok", now=1.0)
    snap = cache.snapshot()
    # Mutating the returned mapping must not affect the cache.
    snap.clear()
    assert "nextcloud_reachable" in cache.snapshot()


def test_is_stale_when_empty():
    assert ReadinessCache(ttl_seconds=30.0).is_stale(now=0.0) is True


def test_is_stale_respects_ttl():
    cache = ReadinessCache(ttl_seconds=30.0)
    cache.update("nextcloud_reachable", True, "ok", now=100.0)

    # Within the TTL window the snapshot is fresh.
    assert cache.is_stale(now=120.0) is False
    # At/after the TTL boundary it is stale.
    assert cache.is_stale(now=130.0) is True


def test_is_stale_if_any_entry_is_old():
    cache = ReadinessCache(ttl_seconds=30.0)
    cache.update("nextcloud_reachable", True, "ok", now=100.0)
    cache.update("qdrant", True, "ok", now=200.0)
    # nextcloud_reachable is well past the TTL even though qdrant is fresh.
    assert cache.is_stale(now=205.0) is True
