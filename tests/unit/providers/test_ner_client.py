"""Unit tests for the gateway NER client.

The client is strict where the rerank client is lenient: a submitted text the
response does not account for would be served as containing no names, i.e.
unredacted. So every malformed shape must raise rather than be skipped.
"""

import json

import httpx
import pytest

from nextcloud_mcp_server.providers.ner import (
    MAX_TEXT_CHARS,
    NerClient,
    NerError,
    windows,
)

pytestmark = pytest.mark.unit

_URL = "https://gw.example/v1/ner"


def _patch_transport(monkeypatch, handler):
    """Same MockTransport shim as test_rerank_client.py (no respx here)."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    original = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return seen


def _ok(payload):
    return lambda request: httpx.Response(200, json=payload)


def _person(start, end, text="ignored"):
    return {"start": start, "end": end, "text": text, "label": "person", "score": 0.9}


async def test_posts_texts_and_reads_names_from_offsets(monkeypatch):
    seen = _patch_transport(
        monkeypatch,
        _ok(
            {
                "results": [
                    # The echoed "text" is ignored; the name comes from the
                    # submitted text via the offsets.
                    {"index": 0, "entities": [_person(5, 16, "KAREN")]},
                    {"index": 1, "entities": []},
                ]
            }
        ),
    )
    found = await NerClient(_URL, "local/m").detect(["Dear Karen Smith,", "none"])

    body = json.loads(seen[0].content)
    assert body["model"] == "local/m"
    assert body["texts"] == ["Dear Karen Smith,", "none"]
    assert body["labels"] == ["person"]
    assert found == [{"Karen Smith"}, set()]


async def test_ignores_non_person_and_out_of_range_entities(monkeypatch):
    _patch_transport(
        monkeypatch,
        _ok(
            {
                "results": [
                    {
                        "index": 0,
                        "entities": [
                            {**_person(0, 3), "label": "organization"},
                            _person(0, 999),
                            _person(True, 3),
                        ],
                    }
                ]
            }
        ),
    )
    assert await NerClient(_URL, "m").detect(["ACME Ltd"]) == [set()]


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"index": 0, "entities": []}]},  # text 1 unaccounted for
        {"results": [{"index": 0, "entities": []}] * 2},  # duplicate index
        {"results": [{"index": 5, "entities": []}]},
        # bool is an int subclass: True must not be read as index 1.
        {"results": [{"index": 0, "entities": []}, {"index": True, "entities": []}]},
        {"results": [{"index": 0}]},
        {"nope": []},
        [],
    ],
)
async def test_incomplete_or_malformed_response_raises(monkeypatch, payload):
    _patch_transport(monkeypatch, _ok(payload))
    with pytest.raises(NerError):
        await NerClient(_URL, "m").detect(["a", "b"])


async def test_http_error_raises(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(503))
    with pytest.raises(NerError, match="HTTP 503"):
        await NerClient(_URL, "m").detect(["a"])


async def test_batches_large_inputs(monkeypatch):
    def handler(request):
        n = len(json.loads(request.content)["texts"])
        return httpx.Response(
            200, json={"results": [{"index": i, "entities": []} for i in range(n)]}
        )

    seen = _patch_transport(monkeypatch, handler)
    found = await NerClient(_URL, "m").detect(["t"] * 70)
    assert len(found) == 70
    assert len(seen) == 3


async def test_oversized_text_is_rejected_before_sending(monkeypatch):
    seen = _patch_transport(monkeypatch, _ok({"results": []}))
    with pytest.raises(NerError):
        await NerClient(_URL, "m").detect(["x" * (MAX_TEXT_CHARS + 1)])
    assert seen == []


async def test_bearer_header_from_token_provider(monkeypatch, mocker):
    seen = _patch_transport(
        monkeypatch, _ok({"results": [{"index": 0, "entities": []}]})
    )
    token_provider = mocker.MagicMock()
    token_provider.get_token = mocker.AsyncMock(return_value="tok-123")
    await NerClient(_URL, "m", token_provider).detect(["a"])
    assert seen[0].headers["authorization"] == "Bearer tok-123"


def test_windows_overlap_and_cover_text():
    text = "".join(chr(65 + i % 26) for i in range(5000))
    parts = windows(text)
    assert all(len(p) <= MAX_TEXT_CHARS for p in parts)
    assert parts[0] + parts[1][200:] + parts[2][200:] == text
    assert windows("short") == ["short"]
