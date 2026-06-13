"""Unit test for the per-tier ingest-queue-depth gauge (Deck #323).

Guards the round-2 fix: a queue that drains to empty (and so drops out of
procrastinate's ``list_queues_async``) must read 0, not its last non-zero value.
"""

import pytest
from pytest import approx

from nextcloud_mcp_server.observability.metrics import update_ingest_queue_depth

pytestmark = pytest.mark.unit

_METRIC = "astrolabe_ingest_queue_depth"


def test_drained_queue_zeroes_not_stale(metric_sample):
    # ocr has a backlog this tick. (pytest.approx: the gauge sample is a float.)
    update_ingest_queue_depth({"ingest-ocr": {"todo": 4}})
    assert metric_sample(_METRIC, {"queue": "ingest-ocr", "status": "todo"}) == approx(
        4
    )

    # Next tick ocr has drained → procrastinate omits it from by_queue entirely.
    update_ingest_queue_depth({"ingest-fast": {"todo": 1}})
    # The gauge must read 0 for the drained queue, not the stale 4.
    assert metric_sample(_METRIC, {"queue": "ingest-ocr", "status": "todo"}) == approx(
        0
    )
    assert metric_sample(_METRIC, {"queue": "ingest-fast", "status": "todo"}) == approx(
        1
    )


def test_none_is_noop(metric_sample):
    update_ingest_queue_depth({"ingest-fast": {"doing": 2}})
    # Memory backend passes None → must not wipe the last published values.
    update_ingest_queue_depth(None)
    assert metric_sample(
        _METRIC, {"queue": "ingest-fast", "status": "doing"}
    ) == approx(2)


def test_all_queues_drained_empty_dict_zeroes(metric_sample):
    # postgres backend with every queue drained → get_ingest_job_counts_by_queue
    # returns {} (list_queues_async drops empty queues). An empty dict is NOT the
    # memory-backend no-op: it must still zero every managed queue's gauge.
    update_ingest_queue_depth({"ingest-fast": {"todo": 9}})
    assert metric_sample(_METRIC, {"queue": "ingest-fast", "status": "todo"}) == approx(
        9
    )
    update_ingest_queue_depth({})
    assert metric_sample(_METRIC, {"queue": "ingest-fast", "status": "todo"}) == approx(
        0
    )
