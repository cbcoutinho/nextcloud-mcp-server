"""Unit test for the per-tier ingest-queue-depth gauge (Deck #323).

Guards the round-2 fix: a queue that drains to empty (and so drops out of
procrastinate's ``list_queues_async``) must read 0, not its last non-zero value.
"""

import pytest

from nextcloud_mcp_server.observability.metrics import update_ingest_queue_depth

pytestmark = pytest.mark.unit

_METRIC = "astrolabe_ingest_queue_depth"


def test_drained_queue_zeroes_not_stale(metric_sample):
    # ocr has a backlog this tick.
    update_ingest_queue_depth({"ingest-ocr": {"todo": 4}})
    assert metric_sample(_METRIC, {"queue": "ingest-ocr", "status": "todo"}) == 4.0

    # Next tick ocr has drained → procrastinate omits it from by_queue entirely.
    update_ingest_queue_depth({"ingest-fast": {"todo": 1}})
    # The gauge must read 0 for the drained queue, not the stale 4.
    assert metric_sample(_METRIC, {"queue": "ingest-ocr", "status": "todo"}) == 0.0
    assert metric_sample(_METRIC, {"queue": "ingest-fast", "status": "todo"}) == 1.0


def test_none_is_noop(metric_sample):
    update_ingest_queue_depth({"ingest-fast": {"doing": 2}})
    # Memory backend passes None → must not wipe the last published values.
    update_ingest_queue_depth(None)
    assert metric_sample(_METRIC, {"queue": "ingest-fast", "status": "doing"}) == 2.0
