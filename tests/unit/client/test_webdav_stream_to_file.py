"""Unit tests for the streaming WebDAV download.

``stream_to_file`` exists so peak memory stops scaling with document size --
``read_file`` buffers the whole body, which is how a 531 MB PDF OOMKilled an
ingest worker mid-download.

The load-bearing property beyond "it writes the file" is that it keeps the two
guards ``_read_complete_body`` already had: the #965 short-read check and the
#1099 content-encoding carve-out. Both paths now share
``_verify_content_length``, and these tests pin that they agree.
"""

from __future__ import annotations

import gzip

import httpx
import pytest

from nextcloud_mcp_server.client.webdav import (
    OversizeDownload,
    WebDAVClient,
    _read_complete_body,
    _verify_content_length,
)

pytestmark = pytest.mark.unit


def _response(body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        content=body,
        headers=headers or {},
        request=httpx.Request("GET", "http://nc/remote.php/dav/files/u/f.pdf"),
    )


def _client(
    body: bytes, headers: dict[str, str] | None = None, chunk: int = 3
) -> WebDAVClient:
    """A WebDAVClient whose transport streams ``body`` back in small chunks."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers=headers or {})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://nc")
    client = WebDAVClient(http, "u")
    # Principal discovery talks to the server; the transport above answers every
    # request identically, so short-circuit it.
    client._principal_id = "u"
    client._principal_discovered = True
    return client


# --- the shared guard: streaming and buffered paths must agree ----------------


@pytest.mark.parametrize(
    ("body", "headers", "raises"),
    [
        (b"12345", {"content-length": "5"}, False),
        (b"123", {"content-length": "5"}, True),  # #965 short read
        (b"123", {"content-length": "5", "content-encoding": "identity"}, True),
        (b"123", {}, False),  # no header (chunked)
        (b"123", {"content-length": "not-a-number"}, False),  # malformed
        (b"123", {"content-length": "-1"}, False),  # degenerate
    ],
)
def test_content_length_guard_matches_buffered_path(body, headers, raises):
    """One implementation, so the two download paths cannot drift apart."""
    if raises:
        with pytest.raises(httpx.RemoteProtocolError):
            _read_complete_body(_response(body, headers), "f.pdf")
        with pytest.raises(httpx.RemoteProtocolError):
            _verify_content_length(_response(body, headers), len(body), "f.pdf")
    else:
        assert _read_complete_body(_response(body, headers), "f.pdf") == body
        # Must not raise for the streaming path either.
        _verify_content_length(_response(body, headers), len(body), "f.pdf")


def test_compressed_response_is_exempt_on_both_paths():
    """#1099: Content-Length is the compressed size, so it cannot be compared.

    Uses genuinely gzipped content because httpx decodes the body transparently,
    so a fake content-encoding header would fail in the decoder rather than
    exercising the carve-out.
    """
    raw = b"decompressed body that is much longer than the compressed form"
    packed = gzip.compress(raw)
    headers = {"content-length": str(len(packed)), "content-encoding": "gzip"}

    # Buffered path: returns the decompressed body without raising, even though
    # len(raw) != the declared (compressed) length.
    assert _read_complete_body(_response(packed, headers), "f.pdf") == raw
    # Streaming path: same exemption, checked against bytes written.
    _verify_content_length(_response(packed, headers), len(raw), "f.pdf")


# --- streaming behaviour ------------------------------------------------------


async def test_streams_body_to_disk(tmp_path):
    body = b"%PDF-1.7" + b"x" * 500
    dest = tmp_path / "out.pdf"

    written, content_type = await _client(
        body, {"content-length": str(len(body)), "content-type": "application/pdf"}
    ).stream_to_file("/f.pdf", dest)

    assert written == len(body)
    assert content_type == "application/pdf"
    assert dest.read_bytes() == body


async def test_short_read_raises_and_removes_partial_file(tmp_path):
    """#965: a truncated download must not be left on disk to parse as valid."""
    dest = tmp_path / "out.pdf"

    with pytest.raises(httpx.RemoteProtocolError):
        await _client(b"short", {"content-length": "9999"}).stream_to_file(
            "/f.pdf", dest
        )

    assert not dest.exists()


async def test_streaming_compressed_response_skips_the_length_check(tmp_path):
    """#1099 end-to-end: a gzipped file must not trip the short-read guard."""
    raw = b"decompressed body, longer than the declared compressed length"
    packed = gzip.compress(raw)
    dest = tmp_path / "out.pdf"

    written, _ = await _client(
        packed, {"content-length": str(len(packed)), "content-encoding": "gzip"}
    ).stream_to_file("/f.pdf", dest)

    # httpx decodes transparently, so what lands on disk is the real document.
    assert dest.read_bytes() == raw
    assert written == len(raw)


async def test_max_bytes_aborts_and_removes_the_file(tmp_path):
    body = b"x" * 5000
    dest = tmp_path / "out.pdf"

    with pytest.raises(OversizeDownload):
        await _client(body, {"content-length": str(len(body))}).stream_to_file(
            "/f.pdf", dest, max_bytes=100
        )

    assert not dest.exists()


async def test_max_bytes_fires_even_when_content_length_lies_small(tmp_path):
    """The reason max_bytes exists: the advertised size cannot be trusted.

    The pre-flight gate can only act on what the server claimed at scan time, so
    a server understating the length would otherwise walk straight past it.
    """
    body = b"x" * 5000
    dest = tmp_path / "out.pdf"

    with pytest.raises(OversizeDownload):
        await _client(body, {"content-length": "10"}).stream_to_file(
            "/f.pdf", dest, max_bytes=100
        )

    assert not dest.exists()


async def test_exactly_at_the_limit_is_allowed(tmp_path):
    body = b"x" * 100
    dest = tmp_path / "out.pdf"

    written, _ = await _client(body, {"content-length": "100"}).stream_to_file(
        "/f.pdf", dest, max_bytes=100
    )

    assert written == 100


def test_make_request_still_carries_the_429_retry():
    """Guard: adding _stream_request must not steal _make_request's decorator.

    While introducing the streaming path, the new method was briefly inserted
    between ``@retry_on_429`` and ``_make_request``, silently moving the retry
    onto the wrong function. Nothing else in the suite would have caught that.
    """
    from nextcloud_mcp_server.client.base import BaseNextcloudClient

    # functools.wraps preserves __name__, so the wrapper is only detectable by
    # the closure the decorator builds around the original function.
    assert BaseNextcloudClient._make_request.__wrapped__ is not None
    assert not hasattr(BaseNextcloudClient._stream_request, "__wrapped__") or (
        BaseNextcloudClient._stream_request.__wrapped__.__name__ == "_stream_request"
    )
