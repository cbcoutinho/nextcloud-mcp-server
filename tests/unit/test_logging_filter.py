"""Unit tests for logging filters."""

import logging

import pytest

from nextcloud_mcp_server.observability.logging_config import HealthCheckFilter


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
