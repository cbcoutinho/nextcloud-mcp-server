"""Regression test for processor_task's exception handler (card 309 / PR #891).

If ``receive_stream.receive()`` raises something other than
``TimeoutError``/``EndOfStream`` before any document is bound, the broad
``except`` handler must not crash on an unbound ``doc_task`` name.
"""

from unittest.mock import MagicMock

import anyio
import pytest

from nextcloud_mcp_server.vector.processor import processor_task


class _ReceiveBoomThenEnd:
    """First receive() raises a non-Timeout error (no doc_task bound yet); the
    second ends the stream so the loop exits."""

    def __init__(self, shutdown: anyio.Event):
        self._calls = 0
        self._shutdown = shutdown

    async def receive(self):
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("transport blew up before any document")
        self._shutdown.set()
        raise anyio.EndOfStream

    def statistics(self):  # pragma: no cover - not reached on the error path
        return MagicMock(current_buffer_used=0)


@pytest.mark.unit
async def test_processor_task_receive_error_does_not_raise_unbound(caplog):
    shutdown = anyio.Event()
    stream = _ReceiveBoomThenEnd(shutdown)

    # Must complete without a NameError leaking out of the except handler.
    with caplog.at_level("ERROR", logger="nextcloud_mcp_server.vector.processor"):
        await processor_task(
            worker_id=0,
            receive_stream=stream,  # type: ignore[arg-type]
            shutdown_event=shutdown,
            nc_client=MagicMock(),
            user_id="alice",
        )

    assert any("RuntimeError" in rec.message for rec in caplog.records)
