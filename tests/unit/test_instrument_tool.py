"""
Unit tests for @instrument_tool decorator.

Tests that the decorator correctly instruments MCP tools with both
Prometheus metrics and OpenTelemetry tracing.
"""

from unittest.mock import MagicMock, patch

import pytest

from nextcloud_mcp_server.observability.metrics import instrument_tool

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_metrics():
    """Mock Prometheus metrics."""
    with (
        patch(
            "nextcloud_mcp_server.observability.metrics.record_tool_call"
        ) as mock_record,
        patch(
            "nextcloud_mcp_server.observability.metrics.record_tool_error"
        ) as mock_error,
    ):
        yield {"record_tool_call": mock_record, "record_tool_error": mock_error}


@pytest.fixture
def mock_tracer():
    """Mock OpenTelemetry tracer."""
    with patch(
        "nextcloud_mcp_server.observability.tracing.trace_operation"
    ) as mock_trace:
        # Configure mock to act as a context manager that allows exceptions to propagate
        mock_trace.return_value.__enter__ = MagicMock(return_value=None)
        mock_trace.return_value.__exit__ = MagicMock(
            return_value=False
        )  # Return False to allow exceptions to propagate
        yield mock_trace


class TestInstrumentToolDecorator:
    """Test the @instrument_tool decorator."""

    async def test_decorator_creates_trace_span(self, mock_tracer, mock_metrics):
        """Test that decorator creates OpenTelemetry span with correct attributes."""

        @instrument_tool
        async def example_tool(query: str, limit: int = 10):
            return {"results": []}

        # Call the tool
        await example_tool(query="test query", limit=5)

        # Verify trace_operation was called with correct parameters
        mock_tracer.assert_called_once()
        call_args = mock_tracer.call_args

        # Check span name
        assert call_args[0][0] == "mcp.tool.example_tool"

        # Check span attributes
        attributes = call_args[1]["attributes"]
        assert attributes["mcp.tool.name"] == "example_tool"
        assert "query" in attributes["mcp.tool.args"]
        assert "test query" in attributes["mcp.tool.args"]
        assert "limit" in attributes["mcp.tool.args"]

        # Verify record_exception parameter
        assert call_args[1]["record_exception"] is True

    async def test_decorator_sanitizes_sensitive_arguments(
        self, mock_tracer, mock_metrics
    ):
        """Test that sensitive arguments are excluded from span attributes."""

        @instrument_tool
        async def example_tool(
            query: str, password: str, token: str, api_key: str, ctx: object
        ):
            return {"success": True}

        # Call with sensitive parameters
        await example_tool(
            query="test",
            password="secret123",
            token="bearer_token",
            api_key="api_key_123",
            ctx=MagicMock(),
        )

        # Verify trace was created
        mock_tracer.assert_called_once()
        attributes = mock_tracer.call_args[1]["attributes"]

        # Check that sensitive fields are NOT in attributes
        tool_args = attributes["mcp.tool.args"]
        assert "password" not in tool_args
        assert "secret123" not in tool_args
        assert "token" not in tool_args
        assert "bearer_token" not in tool_args
        assert "api_key" not in tool_args
        assert "api_key_123" not in tool_args
        assert "ctx" not in tool_args

        # Check that non-sensitive field IS included
        assert "query" in tool_args
        assert "test" in tool_args

    async def test_decorator_limits_argument_string_length(
        self, mock_tracer, mock_metrics
    ):
        """Test that tool arguments are limited to 500 characters."""

        @instrument_tool
        async def example_tool(query: str):
            return {"results": []}

        # Create a very long query string (>500 chars)
        long_query = "x" * 1000

        await example_tool(query=long_query)

        # Verify arguments were truncated
        mock_tracer.assert_called_once()
        attributes = mock_tracer.call_args[1]["attributes"]
        tool_args = attributes["mcp.tool.args"]

        assert len(tool_args) <= 500

    async def test_decorator_records_success_metrics(self, mock_tracer, mock_metrics):
        """Test that successful tool execution records metrics."""

        @instrument_tool
        async def example_tool():
            return {"success": True}

        # Call the tool
        await example_tool()

        # Verify success metrics were recorded
        mock_metrics["record_tool_call"].assert_called_once()
        call_args = mock_metrics["record_tool_call"].call_args
        assert call_args[0][0] == "example_tool"  # tool_name
        assert isinstance(call_args[0][1], float)  # duration
        assert call_args[0][2] == "success"  # status

    async def test_decorator_records_error_metrics(self, mock_tracer, mock_metrics):
        """Test that tool errors are recorded in metrics."""

        @instrument_tool
        async def failing_tool():
            raise ValueError("Test error")

        # Call the tool and expect exception
        with pytest.raises(ValueError, match="Test error"):
            await failing_tool()

        # Verify error metrics were recorded
        mock_metrics["record_tool_call"].assert_called_once()
        call_args = mock_metrics["record_tool_call"].call_args
        assert call_args[0][0] == "failing_tool"  # tool_name
        assert isinstance(call_args[0][1], float)  # duration
        assert call_args[0][2] == "error"  # status

        # Verify error type was recorded
        mock_metrics["record_tool_error"].assert_called_once()
        error_args = mock_metrics["record_tool_error"].call_args
        assert error_args[0][0] == "failing_tool"  # tool_name
        assert error_args[0][1] == "ValueError"  # error_type

    async def test_decorator_preserves_function_metadata(
        self, mock_tracer, mock_metrics
    ):
        """Test that decorator preserves function name and docstring."""

        @instrument_tool
        async def example_tool():
            """This is a test tool."""
            return {"success": True}

        # Verify function metadata is preserved
        assert example_tool.__name__ == "example_tool"
        assert example_tool.__doc__ == "This is a test tool."

    async def test_decorator_preserves_return_value(self, mock_tracer, mock_metrics):
        """Test that decorator returns the original function's return value."""

        @instrument_tool
        async def example_tool(value: int):
            return {"result": value * 2}

        # Call the tool
        result = await example_tool(value=5)

        # Verify return value is unchanged
        assert result == {"result": 10}

    async def test_decorator_with_no_arguments(self, mock_tracer, mock_metrics):
        """Test decorator with tool that takes no arguments."""

        @instrument_tool
        async def no_args_tool():
            return {"status": "ok"}

        # Call the tool
        await no_args_tool()

        # Verify tracing works with no arguments
        mock_tracer.assert_called_once()
        attributes = mock_tracer.call_args[1]["attributes"]

        # tool_args should be None when there are no kwargs
        assert attributes["mcp.tool.args"] is None
