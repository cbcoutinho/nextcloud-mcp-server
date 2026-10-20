"""The request event hook must not fail a request with a streamed body.

``log_request`` evaluated ``request.content`` as a log argument at every log
level. httpx only buffers bytes/str/form/JSON bodies up front; for a streamed
body (an async iterable, or multipart ``files=``) it raises ``RequestNotRead``
there, so the request failed inside the hook before it was sent. Every app
client shares the one ``httpx.AsyncClient`` that carries this hook.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from nextcloud_mcp_server.client import NextcloudClient, log_request, log_response

pytestmark = pytest.mark.unit

HOOK_LOGGER = "nextcloud_mcp_server.client"
PLACEHOLDER = "<streaming; body not read>"


class _Body:
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"chunk-1"
        yield b"chunk-2"


class _CountingBody:
    """Re-iterable async body that counts the chunks pulled from it."""

    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk


async def _send(caplog, level: int, **request_kwargs) -> bytes:
    """PUT through a client carrying ``log_request``; return the bytes sent."""
    received: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(await request.aread())
        return httpx.Response(204)

    caplog.set_level(level, logger=HOOK_LOGGER)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [log_request]},
    ) as client:
        response = await client.put("https://nc.example/x", **request_kwargs)
    assert response.status_code == 204
    return received[0]


async def test_every_app_client_shares_the_hooked_client():
    """Every app client sends through the one hooked ``httpx.AsyncClient``."""
    nc = NextcloudClient(
        base_url="https://nc.example", username="u", auth=httpx.BasicAuth("u", "p")
    )
    try:
        assert nc._client.event_hooks["request"] == [log_request]
        assert nc._client.event_hooks["response"] == [log_response]
        app_clients = {
            name: value._client is nc._client
            for name, value in vars(nc).items()
            if isinstance(getattr(value, "_client", None), httpx.AsyncClient)
        }
        assert "webdav" in app_clients
        assert all(app_clients.values()), app_clients
    finally:
        await nc.close()


async def test_streamed_put_through_nextcloud_client(caplog):
    """A streamed PUT through the real client's ``_make_request``, at INFO."""
    caplog.set_level(logging.INFO, logger=HOOK_LOGGER)
    received: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(await request.aread())
        return httpx.Response(204)

    nc = NextcloudClient(
        base_url="https://nc.example", username="u", auth=httpx.BasicAuth("u", "p")
    )
    try:
        nc._client._transport = httpx.MockTransport(handler)
        response = await nc.webdav._make_request(
            "PUT",
            "/remote.php/dav/files/u/x",
            content=_CountingBody([b"1" * 5, b"2" * 5]),
        )
    finally:
        await nc.close()
    assert response.status_code == 204
    assert received == [b"1" * 5 + b"2" * 5]


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG], ids=["info", "debug"])
async def test_async_iterable_body_is_sent(caplog, level):
    sent = await _send(caplog, level, content=_Body(), headers={"Content-Length": "14"})
    assert sent == b"chunk-1chunk-2"


async def test_async_iterable_body_logged_as_placeholder_at_debug(caplog):
    await _send(
        caplog, logging.DEBUG, content=_Body(), headers={"Content-Length": "14"}
    )
    assert PLACEHOLDER in caplog.text


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG], ids=["info", "debug"])
async def test_multipart_body_is_sent(caplog, level):
    sent = await _send(caplog, level, files={"f": ("a.txt", b"hello")})
    assert b"hello" in sent


async def test_buffered_body_still_logged_at_debug(caplog):
    await _send(caplog, logging.DEBUG, content=b"hello")
    assert "Request body: b'hello'" in caplog.text


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG], ids=["info", "debug"])
async def test_hook_does_not_pull_any_chunk_before_send(caplog, level):
    body = _CountingBody([b"a" * 10, b"b" * 10, b"c" * 10])
    pulled_at_hook: list[int] = []

    async def spy(request: httpx.Request) -> None:
        pulled_at_hook.append(body.pulled)

    received: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(await request.aread())
        return httpx.Response(204)

    caplog.set_level(level, logger=HOOK_LOGGER)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [log_request, spy]},
    ) as client:
        await client.put(
            "https://nc.example/x", content=body, headers={"Content-Length": "30"}
        )
    assert pulled_at_hook == [0]
    assert received == [b"a" * 10 + b"b" * 10 + b"c" * 10]


async def test_hook_never_reads_the_request(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise AssertionError("the hook must not read the request body")

    caplog.set_level(logging.DEBUG, logger=HOOK_LOGGER)
    request = httpx.Request("PUT", "https://nc.example/x", content=_Body())
    monkeypatch.setattr(httpx.Request, "read", boom)
    monkeypatch.setattr(httpx.Request, "aread", boom)
    await log_request(request)
    assert f"Request body: {PLACEHOLDER}" in caplog.text


async def test_other_exceptions_from_content_propagate(caplog):
    """Only ``RequestNotRead`` is swallowed; anything else still surfaces."""

    class _Broken:
        method = "PUT"
        url = "https://nc.example/x"
        headers: dict[str, str] = {}

        @property
        def content(self) -> bytes:
            raise ValueError("not RequestNotRead")

    caplog.set_level(logging.DEBUG, logger=HOOK_LOGGER)
    with pytest.raises(ValueError, match="not RequestNotRead"):
        await log_request(_Broken())  # type: ignore[arg-type]
