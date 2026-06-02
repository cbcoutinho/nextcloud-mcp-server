"""Unit tests for document-parse instrumentation.

Covers two layers:
1. The ``ProcessorRegistry.process()`` boundary — that it records a parse metric
   (success and error) and opens a ``document_processor.parse`` span with the
   expected attributes, while preserving the existing re-raise on failure.
2. The ``record_document_parse`` / ``record_document_chunks`` /
   ``record_vector_sync_processing`` helpers — that they increment the right
   ``astrolabe_*`` Prometheus series (and that an error parse does NOT bump the
   throughput counters).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import REGISTRY

from nextcloud_mcp_server.document_processors.base import (
    DocumentProcessor,
    ProcessingResult,
    ProcessorError,
)
from nextcloud_mcp_server.document_processors.registry import ProcessorRegistry
from nextcloud_mcp_server.observability.metrics import (
    record_document_chunks,
    record_document_escalation,
    record_document_parse,
    record_vector_sync_processing,
)

pytestmark = pytest.mark.unit


def _sample(name: str, labels: dict[str, str]) -> float:
    """Return a Prometheus sample value, treating 'never observed' as 0."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


class _FakeProcessor(DocumentProcessor):
    """Minimal processor for exercising the registry instrumentation."""

    def __init__(
        self,
        *,
        result: ProcessingResult | None = None,
        exc: Exception | None = None,
        proc_name: str = "pymupdf",
        proc_tier: str = "fast",
    ):
        self._result = result
        self._exc = exc
        self._name = proc_name
        self._tier = proc_tier

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> str:
        return self._tier

    @property
    def supported_mime_types(self) -> set[str]:
        return {"application/pdf"}

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: str | None = None,
        options: dict[str, Any] | None = None,
        progress_callback=None,
    ) -> ProcessingResult:
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result

    async def health_check(self) -> bool:
        return True


@pytest.fixture
def mock_tracer():
    """Patch trace_operation in the registry; expose the yielded span."""
    with patch(
        "nextcloud_mcp_server.document_processors.registry.trace_operation"
    ) as mock_trace:
        span = MagicMock()
        mock_trace.return_value.__enter__ = MagicMock(return_value=span)
        mock_trace.return_value.__exit__ = MagicMock(return_value=False)
        mock_trace.span = span
        yield mock_trace


class TestRegistryParseInstrumentation:
    async def test_success_records_metric_and_span(self, mock_tracer):
        result = ProcessingResult(
            text="x" * 1000,
            metadata={"page_count": 50, "file_size": 99},
            processor="pymupdf",
        )
        registry = ProcessorRegistry()
        registry.register(_FakeProcessor(result=result))

        with patch(
            "nextcloud_mcp_server.document_processors.registry.record_document_parse"
        ) as mock_record:
            out = await registry.process(
                b"%PDF-1.7", "application/pdf", filename="x.pdf"
            )

        assert out is result

        # Metric recorded with parsed pages/chars and success status.
        mock_record.assert_called_once()
        args = mock_record.call_args.args
        kwargs = mock_record.call_args.kwargs
        assert args[0] == "pymupdf"  # processor
        assert args[1] == "fast"  # tier
        assert kwargs["pages"] == 50
        assert kwargs["chars"] == 1000
        assert kwargs["status"] == "success"

        # Span opened with the parse name + identifying attributes.
        assert mock_tracer.call_args.args[0] == "document_processor.parse"
        attrs = mock_tracer.call_args.kwargs["attributes"]
        assert attrs["processor.name"] == "pymupdf"
        assert attrs["processor.tier"] == "fast"
        assert attrs["mime_type"] == "application/pdf"
        assert attrs["escalated"] is False
        # Post-parse attributes set on the span.
        mock_tracer.span.set_attribute.assert_any_call("page_count", 50)
        mock_tracer.span.set_attribute.assert_any_call("char_count", 1000)

    async def test_error_records_error_metric_and_reraises(self, mock_tracer):
        registry = ProcessorRegistry()
        registry.register(_FakeProcessor(exc=ProcessorError("boom")))

        with patch(
            "nextcloud_mcp_server.document_processors.registry.record_document_parse"
        ) as mock_record:
            with pytest.raises(ProcessorError):
                await registry.process(b"data", "application/pdf")

        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["status"] == "error"


class TestParseMetricHelpers:
    def test_success_increments_throughput_counters(self):
        labels = {"processor": "uttest-success", "tier": "fast"}
        before_pages = _sample("astrolabe_document_pages_processed_total", labels)
        before_chars = _sample("astrolabe_document_chars_processed_total", labels)
        before_bytes = _sample("astrolabe_document_bytes_processed_total", labels)
        before_total = _sample(
            "astrolabe_document_parse_total", {**labels, "status": "success"}
        )

        record_document_parse(
            "uttest-success",
            "fast",
            1.23,
            pages=50,
            chars=1000,
            byte_size=99,
            status="success",
        )

        assert _sample("astrolabe_document_pages_processed_total", labels) == (
            before_pages + 50
        )
        assert _sample("astrolabe_document_chars_processed_total", labels) == (
            before_chars + 1000
        )
        assert _sample("astrolabe_document_bytes_processed_total", labels) == (
            before_bytes + 99
        )
        assert _sample(
            "astrolabe_document_parse_total", {**labels, "status": "success"}
        ) == (before_total + 1)
        # The duration histogram observed one sample.
        assert (
            _sample(
                "astrolabe_document_parse_duration_seconds_count",
                {**labels, "status": "success"},
            )
            >= 1
        )

    def test_error_does_not_increment_throughput(self):
        labels = {"processor": "uttest-error", "tier": "fast"}
        record_document_parse(
            "uttest-error",
            "fast",
            0.5,
            pages=10,
            chars=10,
            byte_size=10,
            status="error",
        )
        # Error parses count the attempt + duration, but NOT pages/chars/bytes.
        assert _sample("astrolabe_document_pages_processed_total", labels) == 0.0
        assert _sample("astrolabe_document_chars_processed_total", labels) == 0.0
        assert (
            _sample("astrolabe_document_parse_total", {**labels, "status": "error"})
            == 1.0
        )

    def test_record_document_chunks(self):
        labels = {"doc_type": "uttest-chunks"}
        before = _sample("astrolabe_document_chunks_total", labels)
        record_document_chunks("uttest-chunks", 7)
        assert _sample("astrolabe_document_chunks_total", labels) == before + 7

    def test_vector_sync_processing_increments_documents_indexed(self):
        labels = {"source": "uttest-doctype", "status": "success"}
        before = _sample("astrolabe_documents_indexed_total", labels)
        record_vector_sync_processing(0.1, "success", doc_type="uttest-doctype")
        assert _sample("astrolabe_documents_indexed_total", labels) == before + 1

    def test_vector_sync_processing_without_doc_type_is_noop_for_indexed(self):
        # Without doc_type, the per-type counter must not be touched (the legacy
        # mcp_* counter still increments, but that is out of scope here).
        labels = {"source": "uttest-absent", "status": "success"}
        record_vector_sync_processing(0.1, "success")
        assert _sample("astrolabe_documents_indexed_total", labels) == 0.0

    def test_record_document_escalation(self):
        # Dormant until the tiered pipeline lands; pin its correctness now so the
        # first docling/OCR/LLM caller gets a working counter.
        labels = {"from_tier": "fast", "to_tier": "ocr", "reason": "empty_text"}
        before = _sample("astrolabe_document_escalation_total", labels)
        record_document_escalation("fast", "ocr", "empty_text")
        assert _sample("astrolabe_document_escalation_total", labels) == before + 1
