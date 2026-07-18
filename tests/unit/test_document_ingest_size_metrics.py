"""Unit tests for the corpus size-distribution instrumentation.

``astrolabe_document_ingest_size_bytes`` exists so cap, spool and worker-memory
sizing can be read off a dashboard instead of a one-off manual crawl. The
load-bearing property is that it observes sizes **before** the oversize gate --
if it only counted accepted documents it would be blind to the over-cap tail,
which is exactly the part those decisions depend on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nextcloud_mcp_server.observability.metrics import (
    record_document_ingest_rejected,
    record_document_ingest_size,
)
from nextcloud_mcp_server.vector import processor as proc

pytestmark = pytest.mark.unit

# ``metric_sample`` is provided as a shared fixture in tests/unit/conftest.py.

_SIZE_COUNT = "astrolabe_document_ingest_size_bytes_count"
_SIZE_BUCKET = "astrolabe_document_ingest_size_bytes_bucket"
_REJECTED = "astrolabe_document_ingest_rejected_total"


def _settings(max_pdf_mb: float = 50.0) -> SimpleNamespace:
    return SimpleNamespace(document_max_pdf_size_mb=max_pdf_mb)


def _task(size_bytes: int | None, doc_type: str = "file") -> SimpleNamespace:
    return SimpleNamespace(doc_type=doc_type, size_bytes=size_bytes)


def test_records_size_observation(metric_sample):
    before = metric_sample(_SIZE_COUNT, {"doc_type": "file"})

    record_document_ingest_size("file", 5 * 1024 * 1024)

    assert metric_sample(_SIZE_COUNT, {"doc_type": "file"}) == before + 1


def test_unknown_size_is_not_recorded_as_zero(metric_sample):
    """A missing getcontentlength must not pile a false spike into bucket 0."""
    before = metric_sample(_SIZE_COUNT, {"doc_type": "file"})

    record_document_ingest_size("file", 0)

    assert metric_sample(_SIZE_COUNT, {"doc_type": "file"}) == before


def test_large_sizes_land_in_the_tail_buckets(metric_sample):
    """The 1 GiB tail must be resolvable, not collapsed into +Inf.

    The observed corpus reaches 1040 MB, so a histogram that tops out lower
    would hide the documents that actually drive the sizing decisions.
    """
    # prometheus_client renders bucket bounds with Go float formatting
    # ("1.073741824e+09"), so ask it rather than guessing the repr.
    from prometheus_client.utils import floatToGoString

    one_gib = {"doc_type": "file", "le": floatToGoString(1024 * 1024 * 1024)}
    before = metric_sample(_SIZE_BUCKET, one_gib)

    record_document_ingest_size("file", 900 * 1024 * 1024)

    assert metric_sample(_SIZE_BUCKET, one_gib) == before + 1


def test_rejection_counter(metric_sample):
    labels = {"doc_type": "file", "reason": "oversize"}
    before = metric_sample(_REJECTED, labels)

    record_document_ingest_rejected("file", "oversize")

    assert metric_sample(_REJECTED, labels) == before + 1


def test_oversize_document_is_measured_before_it_is_rejected(metric_sample):
    """The whole point: an over-cap document is counted, not silently dropped."""
    size_before = metric_sample(_SIZE_COUNT, {"doc_type": "file"})
    rejected_before = metric_sample(
        _REJECTED, {"doc_type": "file", "reason": "oversize"}
    )

    result = proc.preflight_oversize_result(
        _task(531 * 1024 * 1024), "/big.pdf", _settings()
    )

    assert result is not None and result.metadata["parse_failed_reason"] == "oversize"
    assert metric_sample(_SIZE_COUNT, {"doc_type": "file"}) == size_before + 1
    assert (
        metric_sample(_REJECTED, {"doc_type": "file", "reason": "oversize"})
        == rejected_before + 1
    )


def test_under_cap_document_is_measured_but_not_rejected(metric_sample):
    size_before = metric_sample(_SIZE_COUNT, {"doc_type": "file"})
    rejected_before = metric_sample(
        _REJECTED, {"doc_type": "file", "reason": "oversize"}
    )

    result = proc.preflight_oversize_result(_task(1024), "/small.pdf", _settings())

    assert result is None
    assert metric_sample(_SIZE_COUNT, {"doc_type": "file"}) == size_before + 1
    assert (
        metric_sample(_REJECTED, {"doc_type": "file", "reason": "oversize"})
        == rejected_before
    )


def test_unknown_size_skips_the_gate_entirely(metric_sample):
    """No size means no observation and no gate -- the post-download guard runs."""
    size_before = metric_sample(_SIZE_COUNT, {"doc_type": "file"})

    result = proc.preflight_oversize_result(_task(None), "/unknown.pdf", _settings())

    assert result is None
    assert metric_sample(_SIZE_COUNT, {"doc_type": "file"}) == size_before
