"""Unit tests for the gateway batch OCR client (Deck #332).

HTTP is exercised via an ``httpx.MockTransport`` injected by monkeypatching
``httpx.AsyncClient`` (the repo has no respx dependency).
"""

from typing import Any, cast

import httpx
import pytest

from nextcloud_mcp_server.embedding import gateway_batch_client as gbc

pytestmark = pytest.mark.unit


def _patch_transport(monkeypatch, handler) -> list[httpx.Request]:
    """Route the client's httpx calls through ``handler``; return a list that
    captures each issued request for assertions."""
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        def _wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        kwargs["transport"] = httpx.MockTransport(_wrapped)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def test_base_url_normalization():
    assert gbc.GatewayBatchOcrClient("https://gw", "m")._base == "https://gw/v1"
    assert gbc.GatewayBatchOcrClient("https://gw/v1/", "m")._base == "https://gw/v1"


async def test_submit_posts_one_document_and_returns_job_id(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202, json={"job_id": "mistral/job-1", "status": "pending"}
        )

    seen = _patch_transport(monkeypatch, handler)
    client = gbc.GatewayBatchOcrClient("https://gw", "mistral/mistral-ocr-latest")

    job_id = await client.submit(b"%PDF-1.7", "application/pdf", custom_id="doc-9")

    assert job_id == "mistral/job-1"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/ocr/batch"
    import json

    body = json.loads(req.content)
    assert body["model"] == "mistral/mistral-ocr-latest"
    assert len(body["documents"]) == 1
    assert body["documents"][0]["custom_id"] == "doc-9"
    assert body["documents"][0]["mime_type"] == "application/pdf"
    assert body["documents"][0]["document_b64"]  # base64 present


async def test_submit_sends_bearer_when_token_provider(monkeypatch):
    class _Tok:
        async def get_token(self) -> str:
            return "tok-abc"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"job_id": "mistral/j", "status": "pending"})

    seen = _patch_transport(monkeypatch, handler)
    # _Tok duck-types get_token; cast for the type checker (the client only awaits
    # get_token()).
    client = gbc.GatewayBatchOcrClient(
        "https://gw", "m", token_provider=cast(Any, _Tok())
    )
    await client.submit(b"x", "application/pdf", custom_id="d")
    assert seen[0].headers["Authorization"] == "Bearer tok-abc"


async def test_submit_raises_on_missing_job_id(monkeypatch):
    # A 2xx with no job_id is a gateway contract violation -> actionable error.
    _patch_transport(monkeypatch, lambda r: httpx.Response(202, json={}))
    with pytest.raises(ValueError, match="no job_id"):
        await gbc.GatewayBatchOcrClient("https://gw", "m").submit(
            b"x", "application/pdf", custom_id="d"
        )


async def test_poll_missing_status_is_failed(monkeypatch):
    # A 2xx body without a status field must fail fast, not poll forever.
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json={"total": 1}))
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_failed


async def test_poll_404_raises_job_not_found(monkeypatch):
    # A 404 means the gateway has no record of this job (row purged by retention or
    # orphaned by a pod move). Surface a TYPED OcrBatchJobNotFound — distinct from a
    # transport/5xx — so the caller drops the id and re-submits instead of re-polling
    # a dead id forever (incident 2026-07-03: a doc polled a purged id for ~2.5 days).
    _patch_transport(
        monkeypatch, lambda r: httpx.Response(404, json={"detail": "gone"})
    )
    with pytest.raises(gbc.OcrBatchJobNotFound) as exc:
        await gbc.GatewayBatchOcrClient("https://gw", "m").poll("surya/deadbeef")
    assert exc.value.job_id == "surya/deadbeef"


async def test_poll_pending(monkeypatch):
    _patch_transport(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "pending", "total": 1}),
    )
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_pending and result.pages == []


async def test_poll_succeeded_maps_pages(monkeypatch):
    body = {
        "status": "succeeded",
        "results": [
            {
                "custom_id": "d",
                "pages": [
                    {"index": 1, "markdown": "two"},
                    {"index": 0, "markdown": "one"},
                ],
            }
        ],
    }
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_succeeded
    # Order is preserved as returned; _pages_to_text sorts downstream. The third
    # tuple element is the per-page ``blocks`` (None here — markdown-only backend).
    assert result.pages == [(1, "two", None), (0, "one", None)]


async def test_poll_succeeded_carries_blocks(monkeypatch):
    """surya-style ``blocks`` (layout + normalized bbox) are threaded through the
    poll result so the OCR processor can compute per-block char spans."""
    blocks = [{"html": "<p>two</p>", "bbox": [0.1, 0.2, 0.3, 0.4]}]
    body = {
        "status": "succeeded",
        "results": [
            {
                "custom_id": "d",
                "pages": [{"index": 0, "markdown": "two", "blocks": blocks}],
            }
        ],
    }
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_succeeded
    assert result.pages == [(0, "two", blocks)]


async def test_poll_failed_surfaces_error(monkeypatch):
    _patch_transport(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "failed", "error": "quota"}),
    )
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_failed and result.error == "quota"


async def test_poll_succeeded_with_per_document_error_is_failed(monkeypatch):
    body = {"status": "succeeded", "results": [{"custom_id": "d", "error": "bad page"}]}
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_failed and result.error == "bad page"


async def test_poll_succeeded_empty_pages_is_failed(monkeypatch):
    # A succeeded job that produced zero pages is a per-document failure, not a
    # silent 0-chunk success.
    body = {"status": "succeeded", "results": [{"custom_id": "d", "pages": []}]}
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_failed and result.error == "no pages returned"


async def test_poll_succeeded_no_results_is_failed(monkeypatch):
    _patch_transport(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "succeeded", "results": []}),
    )
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_failed


async def test_poll_raises_on_http_error(monkeypatch):
    _patch_transport(
        monkeypatch, lambda r: httpx.Response(503, json={"detail": "down"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
