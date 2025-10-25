"""Unit tests for progress notification system."""

import time
from unittest.mock import AsyncMock

import anyio
import pytest

pytestmark = pytest.mark.unit


class TestProgressNotification:
    """Test progress notification in document processors."""

    async def test_progress_callback_called_during_processing(self):
        """Test that progress callback is called at intervals during processing."""
        from nextcloud_mcp_server.document_processors.unstructured import (
            UnstructuredProcessor,
        )

        # Mock progress callback to track calls
        progress_callback = AsyncMock()

        # Create processor with 1-second interval for faster testing
        processor = UnstructuredProcessor(
            api_url="http://test:8000",
            timeout=10,
            progress_interval=1,
        )

        # Create a mock event and start time
        stop_event = anyio.Event()
        start_time = time.time()

        # Run the poller for 3 seconds, then stop it
        async def stop_after_delay():
            await anyio.sleep(3.5)
            stop_event.set()

        # Run poller and stopper concurrently
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                processor._run_progress_poller,
                stop_event,
                progress_callback,
                start_time,
            )
            tg.start_soon(stop_after_delay)

        # Verify progress callback was called at least 3 times (1s, 2s, 3s)
        assert progress_callback.call_count >= 3

        # Verify each call had correct structure
        for call in progress_callback.call_args_list:
            # Calls are made with keyword arguments
            assert "progress" in call.kwargs
            assert "total" in call.kwargs
            assert "message" in call.kwargs

            progress = call.kwargs["progress"]
            total = call.kwargs["total"]
            message = call.kwargs["message"]

            assert isinstance(progress, float)
            assert total is None  # Unknown total for unstructured
            assert "Processing document with unstructured" in message
            assert "elapsed" in message

    async def test_progress_poller_stops_when_event_set(self):
        """Test that progress poller stops immediately when event is set."""
        from nextcloud_mcp_server.document_processors.unstructured import (
            UnstructuredProcessor,
        )

        progress_callback = AsyncMock()
        processor = UnstructuredProcessor(
            api_url="http://test:8000",
            timeout=10,
            progress_interval=10,  # Long interval
        )

        stop_event = anyio.Event()
        start_time = time.time()

        # Set event immediately
        stop_event.set()

        # Run poller
        await processor._run_progress_poller(stop_event, progress_callback, start_time)

        # Should not call progress callback since event was already set
        assert progress_callback.call_count == 0

    async def test_progress_callback_exception_handled(self):
        """Test that exceptions in progress callback don't crash the poller."""
        from nextcloud_mcp_server.document_processors.unstructured import (
            UnstructuredProcessor,
        )

        # Mock callback that raises exception
        progress_callback = AsyncMock(side_effect=Exception("Callback error"))

        processor = UnstructuredProcessor(
            api_url="http://test:8000",
            timeout=10,
            progress_interval=1,
        )

        stop_event = anyio.Event()
        start_time = time.time()

        # Run poller for 2 seconds
        async def stop_after_delay():
            await anyio.sleep(2.5)
            stop_event.set()

        # Should not raise exception even though callback fails
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                processor._run_progress_poller,
                stop_event,
                progress_callback,
                start_time,
            )
            tg.start_soon(stop_after_delay)

        # Callback should have been called (and failed) at least twice
        assert progress_callback.call_count >= 2

    async def test_process_without_progress_callback(self):
        """Test that processing works without progress callback (backward compatibility)."""
        from nextcloud_mcp_server.document_processors.unstructured import (
            UnstructuredProcessor,
        )

        processor = UnstructuredProcessor(
            api_url="http://test:8000",
            timeout=10,
            progress_interval=1,
        )

        # Mock the _make_api_request method to avoid actual HTTP call
        from unittest.mock import patch

        from nextcloud_mcp_server.document_processors.base import ProcessingResult

        mock_result = ProcessingResult(
            text="Test content",
            metadata={"test": "data"},
            processor="unstructured",
            success=True,
        )

        with patch.object(
            processor, "_make_api_request", return_value=mock_result
        ) as mock_request:
            # Call process without progress_callback
            result = await processor.process(
                content=b"test", content_type="application/pdf", progress_callback=None
            )

            # Should call _make_api_request directly
            assert result == mock_result
            mock_request.assert_called_once()
