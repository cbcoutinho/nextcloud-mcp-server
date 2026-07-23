"""Unit tests for the DB observability metrics (Deck #678).

These pin the two properties whose absence made a real incident invisible:

1. The duration histograms must resolve latencies well above 1 second. The
   original ``mcp_db_operation_duration_seconds`` topped out at 1.0s, so when
   per-operation latency regressed from 65ms to ~1.9s, ~50% of samples landed
   in ``+Inf`` — p95/p99 were unusable and the regression showed up only in
   Tempo traces.
2. Connection acquisition must be observable independently of execution. A
   single measurement spanning acquire+execute hid a ~600ms connect inside an
   apparently slow insert.
"""

from __future__ import annotations

import pytest

from nextcloud_mcp_server.observability.metrics import (
    db_connect_duration_seconds,
    db_operation_duration_seconds,
    record_db_connect,
    record_db_operation,
)

pytestmark = pytest.mark.unit


def _upper_bounds(histogram, **labels) -> list[str]:
    return [
        sample.labels["le"]
        for metric in histogram.collect()
        for sample in metric.samples
        if sample.name.endswith("_bucket")
        and all(sample.labels.get(k) == v for k, v in labels.items())
    ]


def _bucket_count(histogram, le: str, **labels) -> float:
    for metric in histogram.collect():
        for sample in metric.samples:
            if (
                sample.name.endswith("_bucket")
                and sample.labels.get("le") == le
                and all(sample.labels.get(k) == v for k, v in labels.items())
            ):
                return sample.value
    return 0.0


@pytest.mark.parametrize(
    "histogram",
    [db_operation_duration_seconds, db_connect_duration_seconds],
    ids=["operation", "connect"],
)
def test_duration_buckets_resolve_multi_second_latency(histogram):
    """A multi-second DB stall must be resolvable, not dumped into +Inf.

    Guards the Deck #678 defect directly: with a 1.0s ceiling, a 1.9s insert is
    indistinguishable from a 60s hang.
    """
    record_db_connect("postgresql", 0.001)  # ensure the child metric exists
    record_db_operation("postgresql", "select", 0.001)

    bounds = [
        float(le) for le in _upper_bounds(histogram, db="postgresql") if le != "+Inf"
    ]
    assert bounds, "histogram exposed no finite buckets"
    assert max(bounds) >= 5.0, (
        f"largest finite bucket is {max(bounds)}s — a multi-second stall would "
        "fall into +Inf and be unmeasurable (Deck #678)"
    )
    # The 1s TCP RTO is a common stall floor; keep an edge on each side of it so
    # a retry-shaped latency is distinguishable from slow-but-healthy work.
    assert any(b < 1.0 for b in bounds) and any(b > 1.0 for b in bounds)


def test_slow_operation_lands_in_a_finite_bucket():
    """A 1.9s operation — the observed Deck #678 latency — is counted below +Inf."""
    before = _bucket_count(
        db_operation_duration_seconds, "2.5", db="postgresql", operation="insert"
    )
    record_db_operation("postgresql", "insert", 1.9)
    after = _bucket_count(
        db_operation_duration_seconds, "2.5", db="postgresql", operation="insert"
    )
    assert after == pytest.approx(before + 1), (
        "a 1.9s insert must be resolvable below +Inf"
    )


def test_connect_duration_is_recorded_independently():
    """``record_db_connect`` observes the acquire half on its own histogram."""
    before = _bucket_count(db_connect_duration_seconds, "1.0", db="postgresql")
    record_db_connect("postgresql", 0.6)  # the observed transatlantic handshake
    after = _bucket_count(db_connect_duration_seconds, "1.0", db="postgresql")
    assert after == pytest.approx(before + 1)
