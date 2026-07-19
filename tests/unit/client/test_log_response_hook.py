"""The response event hook must never consume a streamed body.

httpx runs response event hooks BEFORE the body is fetched. ``log_response``
called ``response.aread()`` unconditionally, which meant:

* every response in the app was buffered and stringified regardless of log
  level, purely to serve a DEBUG line nobody saw; and
* a streamed download was fully materialised by the hook before the caller's
  ``aiter_bytes()`` loop ran -- so ``stream_to_file`` was not, in production,
  streaming at all. A 1 GB ingest download cost ~3.6x the file size resident and
  OOMKilled the fast-tier workers.

The load-bearing property here is *not* what gets logged, it is that a streamed
response reaches the caller unconsumed. These tests pin that directly, with the
real hook attached to a real client, because every prior local reproduction
built its own hook-free client and therefore proved nothing about production.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from nextcloud_mcp_server.client import log_response
from nextcloud_mcp_server.client.base import STREAMING_REQUEST_EXTENSION

pytestmark = pytest.mark.unit

HOOK_LOGGER = "nextcloud_mcp_server.client"


class _AsyncStream(httpx.AsyncByteStream):
    """A not-yet-read response body.

    ``httpx.Response(content=...)`` is consumed the moment it is constructed, so
    it cannot show whether the hook read anything. A stream-backed response
    starts with ``is_stream_consumed`` False, which is what makes the assertion
    meaningful -- and it is also what an over-the-wire response actually is.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self):
        yield self._body


def _response(*, streaming: bool, body: bytes = b"payload") -> httpx.Response:
    extensions = {STREAMING_REQUEST_EXTENSION: True} if streaming else {}
    request = httpx.Request("GET", "https://nc/f.pdf", extensions=extensions)
    return httpx.Response(200, stream=_AsyncStream(body), request=request)


async def test_streamed_response_is_left_unconsumed(caplog):
    """The hook must not read a body the caller marked as streaming."""
    response = _response(streaming=True)
    caplog.set_level(logging.DEBUG, logger=HOOK_LOGGER)

    await log_response(response)

    assert not response.is_stream_consumed
    assert "streaming; body not read" in caplog.text


async def test_non_streamed_response_is_still_logged(caplog):
    """Ordinary responses keep the previous behaviour at DEBUG."""
    response = _response(streaming=False, body=b"hello")
    caplog.set_level(logging.DEBUG, logger=HOOK_LOGGER)

    await log_response(response)

    assert response.is_stream_consumed
    assert "hello" in caplog.text


async def test_no_read_when_debug_is_disabled(caplog):
    """With DEBUG off the hook must not touch the body at all.

    This is the half of the bug that made it expensive at INFO in production:
    the read was unconditional while only the logging was gated.
    """
    response = _response(streaming=False, body=b"hello")
    caplog.set_level(logging.INFO, logger=HOOK_LOGGER)

    await log_response(response)

    assert not response.is_stream_consumed
    assert "hello" not in caplog.text


async def test_stream_survives_the_real_hook_end_to_end(tmp_path):
    """A streamed download through a client carrying the REAL hooks.

    This is the test that would have caught the production bug. Asserting on
    ``log_response`` in isolation is not enough -- the failure only appears once
    the hook is attached to the client and something tries to iterate the body.
    """
    body = b"x" * (512 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        # stream=, not content=: a Response built with content= is consumed on
        # construction, so it would look "buffered" no matter what the hook did
        # and the test could never fail.
        return httpx.Response(200, stream=_AsyncStream(body))

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport,
        base_url="https://nc",
        event_hooks={"response": [log_response]},
    )

    received = 0
    request = http.build_request(
        "GET", "/f.pdf", extensions={STREAMING_REQUEST_EXTENSION: True}
    )
    response = await http.send(request, stream=True)
    try:
        # THE assertion. Counting bytes afterwards proves nothing: once httpx
        # has buffered a body it replays it from response.content, so the byte
        # count matches whether or not the hook ate the stream. Only the
        # unconsumed state distinguishes streaming from buffering, and it is
        # what bounds memory.
        assert not response.is_stream_consumed

        async for chunk in response.aiter_bytes():
            received += len(chunk)
    finally:
        await response.aclose()
        await http.aclose()

    assert received == len(body)


async def test_unmarked_stream_would_be_consumed(caplog):
    """Pins WHY the marker is required, so its removal fails loudly.

    Without the extension the hook cannot tell a streamed request apart and
    reads it -- which is precisely the production failure. If a future change
    makes the marker unnecessary (e.g. httpx exposes stream intent on the
    request), this test should be updated deliberately, not silently.
    """
    body = b"y" * 4096

    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, stream=_AsyncStream(body))
    )
    http = httpx.AsyncClient(
        transport=transport,
        base_url="https://nc",
        event_hooks={"response": [log_response]},
    )

    # caplog.set_level, not Logger.setLevel: the latter is a global mutation
    # that outlives the test and would leave this logger at DEBUG for the rest
    # of the session -- reinstating, inside the suite, the very "read on every
    # response regardless of level" cost this change removes.
    caplog.set_level(logging.DEBUG, logger=HOOK_LOGGER)
    request = http.build_request("GET", "/f.pdf")  # deliberately unmarked
    response = await http.send(request, stream=True)
    try:
        assert response.is_stream_consumed
    finally:
        await response.aclose()
        await http.aclose()
