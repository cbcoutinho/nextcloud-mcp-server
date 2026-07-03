"""Unit tests for logging filters."""

import logging

import httpx
import pytest

from nextcloud_mcp_server.document_processors.escalation import (
    BatchPending,
    EscalateError,
)
from nextcloud_mcp_server.observability.logging_config import (
    ExpectedExceptionFilter,
    HealthCheckFilter,
    TraceContextFormatter,
)


def _exc_record(exc: BaseException, name: str = "procrastinate.worker"):
    """Build a LogRecord carrying ``exc`` as exc_info, like ``logger.error(..., exc_info=exc)``."""
    try:
        raise exc
    except Exception as raised:  # noqa: BLE001 - populate __traceback__
        return logging.LogRecord(
            name=name,
            level=logging.ERROR,
            pathname="t.py",
            lineno=1,
            msg="Job ended with status: Error",
            args=(),
            exc_info=(type(raised), raised, raised.__traceback__),
        )


@pytest.mark.unit
class TestHealthCheckFilter:
    """Tests for the HealthCheckFilter."""

    def test_filters_health_live_requests(self):
        """Test that /health/live requests are filtered out."""
        # Create a log record that looks like a uvicorn access log for /health/live
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='127.0.0.1:12345 - "GET /health/live HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )

        filter_instance = HealthCheckFilter()
        assert filter_instance.filter(record) is False

    def test_filters_health_ready_requests(self):
        """Test that /health/ready requests are filtered out."""
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='127.0.0.1:12345 - "GET /health/ready HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )

        filter_instance = HealthCheckFilter()
        assert filter_instance.filter(record) is False

    def test_filters_metrics_requests(self):
        """Test that /metrics requests are filtered out."""
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='127.0.0.1:12345 - "GET /metrics HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )

        filter_instance = HealthCheckFilter()
        assert filter_instance.filter(record) is False

    def test_allows_other_requests(self):
        """Test that non-health-check requests are not filtered."""
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='127.0.0.1:12345 - "GET /mcp/messages HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )

        filter_instance = HealthCheckFilter()
        assert filter_instance.filter(record) is True

    def test_allows_api_requests(self):
        """Test that API requests are not filtered."""
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='127.0.0.1:12345 - "POST /oauth/login HTTP/1.1" 302',
            args=(),
            exc_info=None,
        )

        filter_instance = HealthCheckFilter()
        assert filter_instance.filter(record) is True


@pytest.mark.unit
class TestExpectedExceptionFilter:
    """Tests for ExpectedExceptionFilter — strips tracebacks for expected/handled
    exceptions re-logged by noisy library loggers, keeps them for real bugs."""

    @pytest.mark.parametrize(
        "exc",
        [
            EscalateError(
                from_tier="fast", to_tier="structured", reason="corrupt_glyphs"
            ),
            BatchPending(retry_in=5),
            httpx.ConnectError("All connection attempts failed"),
            httpx.ReadTimeout("timed out"),
            httpx.HTTPStatusError(
                "404",
                request=httpx.Request("GET", "https://embedding-gateway/ocr"),
                response=httpx.Response(404),
            ),
        ],
    )
    def test_strips_traceback_for_expected_exceptions(self, exc):
        """Expected/handled exceptions keep the message but lose the traceback."""
        record = _exc_record(exc)
        filter_instance = ExpectedExceptionFilter()

        assert filter_instance.filter(record) is True  # record is always kept
        assert record.exc_info is None
        assert record.exc_text is None

    def test_strips_traceback_for_expected_exception_group(self):
        """A BaseExceptionGroup of only expected leaves is stripped (anyio path)."""
        group = ExceptionGroup(
            "task group",
            [EscalateError(from_tier="fast", to_tier="ocr", reason="oom")],
        )
        record = _exc_record(group)

        assert ExpectedExceptionFilter().filter(record) is True
        assert record.exc_info is None

    def test_keeps_traceback_for_unexpected_exception(self):
        """A genuine bug keeps its full traceback so it stays debuggable."""
        record = _exc_record(KeyError("boom"))

        assert ExpectedExceptionFilter().filter(record) is True
        assert record.exc_info is not None
        assert record.exc_info[0] is KeyError

    def test_keeps_traceback_for_mixed_exception_group(self):
        """A group mixing an expected signal with a real bug keeps its traceback."""
        group = ExceptionGroup(
            "task group",
            [
                EscalateError(from_tier="fast", to_tier="ocr", reason="oom"),
                KeyError("boom"),
            ],
        )
        record = _exc_record(group)

        assert ExpectedExceptionFilter().filter(record) is True
        assert record.exc_info is not None

    def test_passes_records_without_exception(self):
        """Records with no exc_info are untouched and kept."""
        record = logging.LogRecord(
            name="procrastinate.worker",
            level=logging.INFO,
            pathname="t.py",
            lineno=1,
            msg="Job ended with status: Succeeded",
            args=(),
            exc_info=None,
        )

        assert ExpectedExceptionFilter().filter(record) is True
        assert record.exc_info is None

    def test_formatter_emits_no_exception_field_after_filter(self):
        """End to end: once the filter clears exc_info, the JSON formatter emits
        no ``exception`` field, while an unexpected error still does."""
        formatter = TraceContextFormatter(
            "%(timestamp)s %(level)s %(name)s %(message)s",
        )
        expected = _exc_record(
            EscalateError(from_tier="fast", to_tier="structured", reason="empty_text")
        )
        ExpectedExceptionFilter().filter(expected)
        assert "Traceback" not in formatter.format(expected)

        genuine = _exc_record(ValueError("real bug"))
        ExpectedExceptionFilter().filter(genuine)
        assert "Traceback" in formatter.format(genuine)
