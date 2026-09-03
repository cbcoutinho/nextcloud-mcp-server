"""Unit tests for the gateway batch OCR client (Deck #332).

HTTP is exercised via an ``httpx.MockTransport`` injected by monkeypatching
``httpx.AsyncClient`` (the repo has no respx dependency).
"""

from typing import Any, cast

import httpx
import pytest

from nextcloud_mcp_server.providers import gateway_batch as gbc

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
    client = gbc.GatewayBatchOcrClient("https://gw", "m")
    with pytest.raises(ValueError, match="no job_id"):
        await client.submit(b"x", "application/pdf", custom_id="d")


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
    client = gbc.GatewayBatchOcrClient("https://gw", "m")
    with pytest.raises(gbc.OcrBatchJobNotFound) as exc:
        await client.poll("surya/deadbeef")
    assert exc.value.job_id == "surya/deadbeef"


async def test_poll_pending(monkeypatch):
    # No Retry-After header -> retry_after is None (caller falls back to its own
    # configured poll interval).
    _patch_transport(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "pending", "total": 1}),
    )
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_pending and result.pages == []
    assert result.retry_after is None


async def test_poll_pending_parses_retry_after_header(monkeypatch):
    # A pending poll carrying the gateway's Retry-After (delta-seconds) surfaces it
    # on the result so the caller can back off (anti-storm, Deck #523). This is the
    # layer doing the actual HTTP header parsing.
    _patch_transport(
        monkeypatch,
        lambda r: httpx.Response(
            202, json={"status": "pending"}, headers={"Retry-After": "30"}
        ),
    )
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_pending
    assert result.retry_after == 30


async def test_poll_pending_ignores_malformed_retry_after_header(monkeypatch):
    # An HTTP-date-formatted Retry-After (which _parse_retry_after intentionally does
    # NOT support) — or any non-integer — degrades to None rather than raising.
    _patch_transport(
        monkeypatch,
        lambda r: httpx.Response(
            202,
            json={"status": "pending"},
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
        ),
    )
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/j")
    assert result.is_pending
    assert result.retry_after is None


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
    client = gbc.GatewayBatchOcrClient("https://gw", "m")
    with pytest.raises(httpx.HTTPStatusError):
        await client.poll("mistral/j")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("30", 30),
        ("  45  ", 45),  # surrounding whitespace tolerated
        (None, None),  # header absent
        ("0", None),  # non-positive -> no back-off signal
        ("-5", None),  # negative -> ignored
        ("30.5", None),  # non-integer -> ignored (we send whole seconds)
        ("soon", None),  # non-numeric -> ignored
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # HTTP-date form unsupported
    ],
)
def test_parse_retry_after(raw, expected):
    assert gbc._parse_retry_after(raw) == expected


def _capture_timeouts(monkeypatch, handler) -> list[httpx.Timeout]:
    """Like ``_patch_transport`` but records the ``timeout`` each AsyncClient is
    constructed with, so the submit/poll split can be asserted."""
    timeouts: list[httpx.Timeout] = []
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        timeouts.append(kwargs["timeout"])
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return timeouts


async def test_submit_and_poll_use_separate_read_timeouts(monkeypatch):
    """Submit must NOT share the poll's control-plane read timeout.

    A poll is a small status read; a submit uploads the whole base64-inflated
    document and the gateway stages it to object storage before answering. Losing
    that race strands the job gateway-side: the caller never learns the job_id,
    ``insert_pending`` never runs, and the retry submits a SECOND job for the same
    document. Collapsing these two constants back together would silently restore
    that duplicate-GPU-work bug, so pin them apart.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "surya/j", "status": "pending"})
        return httpx.Response(202, json={"status": "pending"})

    timeouts = _capture_timeouts(monkeypatch, handler)
    client = gbc.GatewayBatchOcrClient("https://gw", "surya/surya-ocr-2")

    await client.submit(b"%PDF-1.7", "application/pdf", custom_id="doc-1")
    await client.poll("surya/j")

    submit_timeout, poll_timeout = timeouts
    assert submit_timeout.read == gbc._BATCH_SUBMIT_TIMEOUT_SECONDS
    assert poll_timeout.read == gbc._BATCH_REQUEST_TIMEOUT_SECONDS
    assert submit_timeout.read > poll_timeout.read
    # The connect timeout stays short and shared -- neither call waits on OCR.
    assert submit_timeout.connect == poll_timeout.connect
    assert submit_timeout.connect == gbc._BATCH_CONNECT_TIMEOUT_SECONDS


# --- Deck #1192: a poll that gets no answer must not terminalise the document ---


async def test_poll_read_timeout_is_pending(monkeypatch):
    """A ReadTimeout on the poll reads as PENDING, not as a hard failure.

    Propagating it burned TieredEscalationStrategy's transient-attempt budget and
    then DROPPED the document, while the gateway may still have been OCRing it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    _patch_transport(monkeypatch, handler)
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/job-1")

    assert result.is_pending
    assert result.retry_after is None


async def test_poll_5xx_is_pending(monkeypatch):
    """A gateway 5xx (e.g. its upstream provider leg failing) is likewise "no
    answer yet", not a terminal job state."""
    _patch_transport(
        monkeypatch, lambda r: httpx.Response(502, json={"detail": "poll failed"})
    )
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/job-1")

    assert result.is_pending


async def test_poll_429_is_pending_and_honours_retry_after(monkeypatch):
    """ "Poll slower" is a statement about us, never about the job. Hard-failing it
    would reintroduce the drop for a gateway that rate-limits polls under load."""
    _patch_transport(
        monkeypatch,
        lambda r: httpx.Response(429, json={}, headers={"Retry-After": "90"}),
    )
    result = await gbc.GatewayBatchOcrClient("https://gw", "m").poll("mistral/job-1")

    assert result.is_pending
    assert result.retry_after == 90


async def test_poll_token_provider_failure_still_raises(monkeypatch):
    """The token provider talks to the OIDC issuer, not the gateway, and raises the
    same httpx types the PENDING remap catches. An M2M outage must surface as an auth
    fault — not as "the gateway says the job is still running" on every document."""

    class _BrokenTok:
        async def get_token(self) -> str:
            raise httpx.ConnectError("token endpoint unreachable")

    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json={}))
    client = gbc.GatewayBatchOcrClient(
        "https://gw", "m", token_provider=cast(Any, _BrokenTok())
    )

    with pytest.raises(httpx.ConnectError):
        await client.poll("mistral/job-1")


async def test_poll_non_404_4xx_still_raises(monkeypatch):
    """A client-side fault is a real error and must keep propagating; 404 (unknown
    job) and 429 (poll slower) have their own handling."""
    _patch_transport(monkeypatch, lambda r: httpx.Response(403, json={}))
    client = gbc.GatewayBatchOcrClient("https://gw", "m")

    with pytest.raises(httpx.HTTPStatusError):
        await client.poll("mistral/job-1")
