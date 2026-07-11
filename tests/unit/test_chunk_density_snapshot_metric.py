"""Unit tests for the current-corpus chunk-density snapshot metric.

Covers the GaugeHistogram exposition path in ``observability/metrics.py``:

- ``density_bucket_index`` — the shared bucketing used by the snapshot publisher.
- ``update_qdrant_chunk_density_snapshot`` → ``_ChunkDensitySnapshotCollector`` —
  the non-cumulative tally is converted to cumulative ``le`` buckets and exposed
  as ``astrolabe_qdrant_chunk_density_chunks_per_mb_current`` (a GaugeHistogram),
  plus the ``uncovered`` and ``truncated`` coverage gauges.
"""

from __future__ import annotations

import pytest

from nextcloud_mcp_server.observability.metrics import (
    CHUNK_DENSITY_BUCKETS,
    density_bucket_index,
    update_qdrant_chunk_density_snapshot,
)

pytestmark = pytest.mark.unit

_CURRENT = "astrolabe_qdrant_chunk_density_chunks_per_mb_current"
_N_SLOTS = len(CHUNK_DENSITY_BUCKETS) + 1


def _tally(*densities: float) -> tuple[list[float], float]:
    """Build a (bucket_counts, gsum) tally from raw density observations."""
    counts = [0.0] * _N_SLOTS
    gsum = 0.0
    for d in densities:
        counts[density_bucket_index(d)] += 1
        gsum += d
    return counts, gsum


class TestDensityBucketIndex:
    def test_value_maps_to_first_edge_at_or_above(self):
        # edges: 1, 5, 10, 20, 40, 60, 91, 120, 160, 200, 300, 500
        assert density_bucket_index(0.5) == 0  # <= 1
        assert density_bucket_index(1) == 0  # boundary, <= 1
        assert density_bucket_index(3) == 1  # <= 5
        assert density_bucket_index(100) == 7  # <= 120
        assert density_bucket_index(91) == 6  # boundary, <= 91

    def test_overflow_goes_to_inf_slot(self):
        assert density_bucket_index(10_000) == len(CHUNK_DENSITY_BUCKETS)


class TestUpdateSnapshot:
    def test_cumulative_buckets_gcount_gsum(self, metric_sample):
        # note: densities 3 (-> le=5) and 100 (-> le=120); gsum=103, gcount=2.
        update_qdrant_chunk_density_snapshot({"note": _tally(3.0, 100.0)})

        labels = {"doc_type": "note"}
        # Below the first observation nothing has accumulated yet.
        assert metric_sample(f"{_CURRENT}_bucket", {**labels, "le": "1"}) == 0.0
        # The le=5 observation shows from le=5 onward (cumulative).
        assert metric_sample(f"{_CURRENT}_bucket", {**labels, "le": "5"}) == 1.0
        assert metric_sample(f"{_CURRENT}_bucket", {**labels, "le": "91"}) == 1.0
        # The le=120 observation joins the cumulative count at 120.
        assert metric_sample(f"{_CURRENT}_bucket", {**labels, "le": "120"}) == 2.0
        assert metric_sample(f"{_CURRENT}_bucket", {**labels, "le": "+Inf"}) == 2.0
        assert metric_sample(f"{_CURRENT}_gcount", labels) == 2.0
        assert metric_sample(f"{_CURRENT}_gsum", labels) == pytest.approx(103.0)

    def test_update_replaces_previous_snapshot(self, metric_sample):
        update_qdrant_chunk_density_snapshot({"note": _tally(3.0, 3.0, 3.0)})
        assert metric_sample(f"{_CURRENT}_gcount", {"doc_type": "note"}) == 3.0
        # A fresh snapshot fully replaces — not accumulates.
        update_qdrant_chunk_density_snapshot({"note": _tally(3.0)})
        assert metric_sample(f"{_CURRENT}_gcount", {"doc_type": "note"}) == 1.0

    def test_uncovered_and_truncated_gauges(self, metric_sample):
        update_qdrant_chunk_density_snapshot(
            {"note": _tally(3.0)},
            uncovered={"file": 4, "deck_card": 1},
            truncated=True,
        )
        assert (
            metric_sample(
                "astrolabe_qdrant_chunk_density_uncovered_documents",
                {"doc_type": "file"},
            )
            == 4.0
        )
        assert (
            metric_sample("astrolabe_qdrant_chunk_density_snapshot_truncated", {})
            == 1.0
        )

    def test_uncovered_gauge_reset_between_snapshots(self, metric_sample):
        update_qdrant_chunk_density_snapshot(
            {"note": _tally(3.0)}, uncovered={"file": 9}
        )
        assert (
            metric_sample(
                "astrolabe_qdrant_chunk_density_uncovered_documents",
                {"doc_type": "file"},
            )
            == 9.0
        )
        # Next snapshot has no uncovered files — the stale series must clear.
        update_qdrant_chunk_density_snapshot({"note": _tally(3.0)}, uncovered={})
        assert (
            metric_sample(
                "astrolabe_qdrant_chunk_density_uncovered_documents",
                {"doc_type": "file"},
            )
            == 0.0
        )
        assert (
            metric_sample("astrolabe_qdrant_chunk_density_snapshot_truncated", {})
            == 0.0
        )
