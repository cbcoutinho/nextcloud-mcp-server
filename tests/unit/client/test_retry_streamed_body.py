"""A retried request must resend its whole body, or not be retried at all.

``retry_on_429`` re-runs ``_make_request`` with the same keyword arguments, and
httpx builds a new request from them each time. A one-shot body -- an async
generator -- is used up by the first attempt, so a chunk-encoded retry went
out EMPTY and nothing raised: on a WebDAV PUT, a 429 then 204 would overwrite
the file with zero bytes.

These tests send through a plain ``httpx.AsyncClient`` with no event hooks, so
they don't depend on the request log hook's handling of streamed bodies.
"""

from __future__ import annotations

import io
import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from nextcloud_mcp_server.client import base
from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit

PATH = "/remote.php/dav/files/u/x"


@pytest.fixture(autouse=True)
def no_sleep(mocker):
    return mocker.patch("nextcloud_mcp_server.client.base.anyio.sleep")


async def _one_shot() -> AsyncIterator[bytes]:
    yield b"one-"
    yield b"shot"


class _Reiterable:
    """Starts over on every ``async for``, yet is still not retried."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"re-"
        yield b"iterable"


class _SharedGenerator:
    """Only ``__aiter__``, handing out one shared generator: really one-shot."""

    def __init__(self) -> None:
        self._gen = _one_shot()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._gen


class _OneShotStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self._gen = _one_shot()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._gen


class _NonSeekable(io.RawIOBase):
    def __init__(self) -> None:
        self._data = b"part-body"

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        n = len(self._data[: len(buffer)])
        buffer[:n] = self._data[:n]
        self._data = self._data[n:]
        return n


async def _put(
    statuses: list[int], url: str = PATH, **request_kwargs
) -> tuple[int, list[bytes]]:
    """PUT via ``_make_request``; the transport answers ``statuses`` in turn."""
    sent: list[bytes] = []
    answers = iter(statuses)

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(await request.aread())
        return httpx.Response(next(answers))

    async with httpx.AsyncClient(
        base_url="https://nc.example", transport=httpx.MockTransport(handler)
    ) as client:
        try:
            response = await WebDAVClient(client, "u")._make_request(
                "PUT", url, **request_kwargs
            )
        except httpx.HTTPStatusError as e:
            return e.response.status_code, sent
    return response.status_code, sent


@pytest.mark.parametrize("status", [429, 423])
async def test_one_shot_body_is_not_retried_empty(status, no_sleep, mocker):
    retry_metric = mocker.patch.object(base, "record_nextcloud_api_retry")
    code, sent = await _put([status, 204], content=_one_shot())
    assert code == status
    assert sent == [b"one-shot"]
    no_sleep.assert_not_awaited()
    retry_metric.assert_not_called()


@pytest.mark.parametrize(
    "request_kwargs",
    [
        pytest.param(lambda: {"content": _Reiterable()}, id="reiterable-aiter"),
        pytest.param(lambda: {"content": _SharedGenerator()}, id="shared-generator"),
        pytest.param(lambda: {"content": _OneShotStream()}, id="async-byte-stream"),
        pytest.param(
            lambda: {"data": _one_shot()},
            id="data-alias",
            marks=pytest.mark.filterwarnings("ignore:Use 'content=:DeprecationWarning"),
        ),
    ],
)
async def test_streamed_body_is_not_retried(request_kwargs):
    code, sent = await _put([429, 204], **request_kwargs())
    assert code == 429
    assert len(sent) == 1
    assert b"" not in sent


@pytest.mark.parametrize(
    "request_kwargs",
    [
        pytest.param({"content": b"bytes-body"}, id="bytes"),
        pytest.param({"content": "str-body"}, id="str"),
        pytest.param({"data": {"a": "b"}}, id="form"),
        pytest.param({"json": {"a": 1}}, id="json"),
    ],
)
async def test_buffered_body_is_retried_whole(request_kwargs):
    code, sent = await _put([429, 204], **request_kwargs)
    assert code == 204
    assert len(sent) == 2
    assert sent[0] == sent[1] != b""


async def test_seekable_multipart_file_is_rewound_and_retried_whole():
    f = io.BytesIO(b"HEADER-rest-of-file")
    f.read(7)
    code, sent = await _put([429, 204], files={"f": ("a.txt", f)})
    assert code == 204
    assert len(sent) == 2
    assert all(b"HEADER-rest-of-file" in body for body in sent)


async def test_non_seekable_multipart_file_is_not_retried():
    code, sent = await _put([429, 204], files={"f": ("a.bin", _NonSeekable())})
    assert code == 429
    assert len(sent) == 1
    assert b"part-body" in sent[0]


async def test_files_given_as_generator_still_sends_the_file():
    files = (t for t in [("f", ("a.txt", b"genfile"))])
    code, sent = await _put([429, 204], files=files)
    assert code == 204
    assert len(sent) == 2
    assert all(b"genfile" in body for body in sent)


async def test_warning_logs_method_and_path_only(caplog):
    caplog.set_level(logging.WARNING, logger=base.__name__)
    await _put(
        [429],
        url="https://alice:s3cret@nc.example/remote.php/dav/files/u/x?token=QTOKEN",
        content=_one_shot(),
    )
    [message] = [r.getMessage() for r in caplog.records if "replayed" in r.message]
    assert "PUT /remote.php/dav/files/u/x" in message
    assert "s3cret" not in message
    assert "alice" not in message
    assert "QTOKEN" not in message
