"""Unit tests for the dense-vector RAM cost observability helpers (card #624).

Covers the pure estimate helper and the two ingest-time recorders in
``observability/metrics.py``:

- ``estimate_vector_bytes`` — the deterministic RAM model
  (``chunks * dim * 4 * overhead``), with keyword/empty docs contributing 0.
- ``record_estimated_vector_bytes`` — the per-document RAM counter.
- ``record_chunk_density`` — the chunks-per-MB "density risk" histogram.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nextcloud_mcp_server.observability.metrics import (
    DENSE_VECTOR_BYTES_PER_DIMENSION,
    estimate_vector_bytes,
    record_chunk_density,
    record_estimated_vector_bytes,
)
from nextcloud_mcp_server.vector import processor as proc

pytestmark = pytest.mark.unit

# ``metric_sample`` is provided as a shared fixture in tests/unit/conftest.py.


class TestEstimateVectorBytes:
    def test_basic_float32_times_overhead(self):
        # 10 chunks * 1536 dims * 4 bytes (float32) * 1.5 overhead.
        assert estimate_vector_bytes(10, 1536, 1.5) == pytest.approx(
            10 * 1536 * DENSE_VECTOR_BYTES_PER_DIMENSION * 1.5
        )

    def test_overhead_of_one_is_raw_footprint(self):
        assert estimate_vector_bytes(4, 384, 1.0) == pytest.approx(
            4 * 384 * DENSE_VECTOR_BYTES_PER_DIMENSION
        )

    def test_zero_chunks_is_zero(self):
        # A keyword-only document embeds no dense vector → no RAM.
        assert estimate_vector_bytes(0, 1536, 1.5) == pytest.approx(0.0)

    def test_negative_chunks_is_zero(self):
        assert estimate_vector_bytes(-5, 1536, 1.5) == pytest.approx(0.0)

    def test_zero_dimension_is_zero(self):
        assert estimate_vector_bytes(10, 0, 1.5) == pytest.approx(0.0)


class TestRecordEstimatedVectorBytes:
    def test_positive_increments_counter(self, metric_sample):
        labels = {"doc_type": "ut-ram-file"}
        before = metric_sample("astrolabe_estimated_vector_bytes_total", labels)
        record_estimated_vector_bytes("ut-ram-file", 92160.0)
        assert metric_sample(
            "astrolabe_estimated_vector_bytes_total", labels
        ) == pytest.approx(before + 92160.0)

    def test_zero_is_noop(self, metric_sample):
        # Keyword docs pass a 0 estimate and must not advance the counter.
        labels = {"doc_type": "ut-ram-keyword"}
        record_estimated_vector_bytes("ut-ram-keyword", 0.0)
        assert metric_sample(
            "astrolabe_estimated_vector_bytes_total", labels
        ) == pytest.approx(0.0)


class TestRecordChunkDensity:
    def test_observes_chunks_per_mb(self, metric_sample):
        labels = {"doc_type": "ut-density-file"}
        # 91 chunks from exactly 1 MB of source → 91 chunks/MB, the note's
        # born-digital baseline.
        record_chunk_density("ut-density-file", 91, 1_000_000)
        assert metric_sample(
            "astrolabe_document_chunk_density_chunks_per_mb_count", labels
        ) == pytest.approx(1.0)
        assert metric_sample(
            "astrolabe_document_chunk_density_chunks_per_mb_sum", labels
        ) == pytest.approx(91.0)

    def test_dense_low_fill_doc_lands_in_upper_bucket(self, metric_sample):
        # 200 chunks from 0.5 MB → 400 chunks/MB, well into the risky tail.
        labels = {"doc_type": "ut-density-dense"}
        record_chunk_density("ut-density-dense", 200, 500_000)
        # The 91-chunks/MB bucket must NOT capture a 400/MB observation...
        assert metric_sample(
            "astrolabe_document_chunk_density_chunks_per_mb_bucket",
            {**labels, "le": "91.0"},
        ) == pytest.approx(0.0)
        # ...but the 500/MB bucket must.
        assert metric_sample(
            "astrolabe_document_chunk_density_chunks_per_mb_bucket",
            {**labels, "le": "500.0"},
        ) == pytest.approx(1.0)

    def test_zero_source_bytes_is_noop(self, metric_sample):
        labels = {"doc_type": "ut-density-zerobytes"}
        record_chunk_density("ut-density-zerobytes", 5, 0)
        assert metric_sample(
            "astrolabe_document_chunk_density_chunks_per_mb_count", labels
        ) == pytest.approx(0.0)

    def test_zero_chunks_is_noop(self, metric_sample):
        labels = {"doc_type": "ut-density-zerochunks"}
        record_chunk_density("ut-density-zerochunks", 0, 1_000_000)
        assert metric_sample(
            "astrolabe_document_chunk_density_chunks_per_mb_count", labels
        ) == pytest.approx(0.0)


class TestRecordIngestVectorCost:
    """The best-effort ingest hook wiring (processor._record_ingest_vector_cost)."""

    @pytest.fixture
    def spies(self, monkeypatch) -> dict[str, MagicMock]:
        s = {
            "record_estimated_vector_bytes": MagicMock(),
            "record_chunk_density": MagicMock(),
        }
        for name, mock in s.items():
            monkeypatch.setattr(proc, name, mock)
        monkeypatch.setattr(
            proc,
            "get_embedding_service",
            lambda: SimpleNamespace(get_dimension=lambda: 1024),
        )
        return s

    def test_hybrid_records_estimate_and_density(self, spies) -> None:
        proc._record_ingest_vector_cost(
            doc_type="file",
            chunk_count=10,
            source_bytes=1_000_000,
            dense_for_doc=True,
            overhead=1.5,
        )
        # Estimate = 10 * 1024 * 4 * 1.5.
        spies["record_estimated_vector_bytes"].assert_called_once_with(
            "file", 10 * 1024 * 4 * 1.5
        )
        spies["record_chunk_density"].assert_called_once_with("file", 10, 1_000_000)

    def test_keyword_skips_estimate_but_records_density(self, spies) -> None:
        proc._record_ingest_vector_cost(
            doc_type="note",
            chunk_count=4,
            source_bytes=500_000,
            dense_for_doc=False,
            overhead=1.5,
        )
        # Keyword docs embed no dense vector — no RAM estimate row...
        spies["record_estimated_vector_bytes"].assert_not_called()
        # ...but density is still observed (it's a property of the content).
        spies["record_chunk_density"].assert_called_once_with("note", 4, 500_000)

    def test_exception_never_propagates(self, monkeypatch) -> None:
        # A failure in the hook must not break indexing — it is swallowed+logged.
        monkeypatch.setattr(
            proc,
            "get_embedding_service",
            MagicMock(side_effect=RuntimeError("provider down")),
        )
        density = MagicMock()
        monkeypatch.setattr(proc, "record_chunk_density", density)

        # Must not raise.
        proc._record_ingest_vector_cost(
            doc_type="file",
            chunk_count=10,
            source_bytes=1_000_000,
            dense_for_doc=True,
            overhead=1.5,
        )
        # The exception short-circuited before the density call.
        density.assert_not_called()
