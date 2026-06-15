"""Unit tests for the tier-3 OCR processor + backend selection."""

from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from nextcloud_mcp_server.document_processors import ocr

pytestmark = pytest.mark.unit


def _settings(**kw) -> Any:  # a Settings stand-in (only the read fields matter)
    base = dict(
        document_ocr_provider="auto",
        document_ocr_model="mistral/mistral-ocr-latest",
        document_ocr_timeout_seconds=180.0,
        document_ocr_mode="sync",
        document_ocr_batch_poll_seconds=120,
        document_ocr_batch_max_wait_seconds=86400,
        embedding_gateway_url=None,
        embedding_gateway_client_id=None,
        embedding_gateway_client_secret=None,
        embedding_gateway_token_url=None,
        embedding_gateway_scope=None,
        mistral_api_key=None,
        mistral_base_url=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- _pages_to_text ----------------------------------------------------------


def test_pages_to_text_orders_and_exact_boundaries():
    text, boundaries = ocr._pages_to_text([(1, "B"), (0, "A")])  # out of order
    assert text == "A\n\nB"
    assert boundaries[0] == {"page": 1, "start_offset": 0, "end_offset": 1}
    assert boundaries[1]["page"] == 2
    # contiguous + offsets index exactly into the text
    assert boundaries[0]["end_offset"] <= boundaries[1]["start_offset"]
    assert boundaries[-1]["end_offset"] == len(text)


# --- backend selection -------------------------------------------------------


def test_build_backend_none():
    assert ocr.build_ocr_backend(_settings(document_ocr_provider="none")) is None


def test_build_backend_gateway():
    b = ocr.build_ocr_backend(
        _settings(document_ocr_provider="gateway", embedding_gateway_url="http://gw")
    )
    assert isinstance(b, ocr._GatewayOcrBackend)


def test_build_backend_mistral():
    b = ocr.build_ocr_backend(
        _settings(document_ocr_provider="mistral", mistral_api_key="k")
    )
    assert isinstance(b, ocr._MistralOcrBackend)


def test_build_backend_auto_prefers_gateway():
    b = ocr.build_ocr_backend(
        _settings(embedding_gateway_url="http://gw", mistral_api_key="k")
    )
    assert isinstance(b, ocr._GatewayOcrBackend)


def test_build_backend_auto_none_configured():
    assert ocr.build_ocr_backend(_settings()) is None


def test_build_backend_gateway_missing_m2m_raises():
    # client_id set but token_url/secret missing -> explicit ValueError (not a
    # stripped assert), surfaced on backend resolution.
    with pytest.raises(ValueError, match="EMBEDDING_GATEWAY_TOKEN_URL"):
        ocr.build_ocr_backend(
            _settings(
                document_ocr_provider="gateway",
                embedding_gateway_url="http://gw",
                embedding_gateway_client_id="cid",
            )
        )


def test_gateway_backend_url_normalization():
    b = ocr._GatewayOcrBackend("http://gw", "mistral/mistral-ocr-latest")
    assert b._url == "http://gw/v1/ocr"
    b2 = ocr._GatewayOcrBackend("http://gw/v1/", "m")
    assert b2._url == "http://gw/v1/ocr"


# --- OcrProcessor ------------------------------------------------------------


async def test_processor_unsupported_when_no_backend(monkeypatch):
    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_provider="none")
    )
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s: None)
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "unsupported"


async def test_processor_success(monkeypatch):
    class _FakeBackend:
        async def ocr(self, content, mime_type):
            return "hello world", [{"page": 1, "start_offset": 0, "end_offset": 11}]

    monkeypatch.setattr(ocr, "get_settings", lambda: _settings())
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s: _FakeBackend())
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is True
    assert r.text == "hello world"
    assert r.metadata["page_count"] == 1
    assert r.processor == "ocr"


async def test_processor_backend_error_returns_success_false(monkeypatch):
    class _BoomBackend:
        async def ocr(self, content, mime_type):
            raise RuntimeError("api down")

    monkeypatch.setattr(ocr, "get_settings", lambda: _settings())
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s: _BoomBackend())
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "error"


async def test_processor_timeout_returns_timeout_reason(monkeypatch):
    """A backend TimeoutError gets its own reason bucket (not 'error')."""

    class _TimeoutBackend:
        async def ocr(self, content, mime_type):
            raise TimeoutError

    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_timeout_seconds=5.0)
    )
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s: _TimeoutBackend())
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "timeout"
    assert "timed out" in (r.error or "")


async def test_gateway_httpx_timeout_maps_to_timeout_reason(monkeypatch):
    """A gateway httpx.ReadTimeout (not a builtin TimeoutError) must still map to
    parse_failed_reason='timeout', not 'error'."""
    import httpx

    class _HttpxTimeoutBackend:
        async def ocr(self, content, mime_type):
            raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_timeout_seconds=5.0)
    )
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s: _HttpxTimeoutBackend())
    r = await ocr.OcrProcessor().process(b"%PDF-1.7", "application/pdf")
    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "timeout"
    assert "timed out" in (r.error or "")


async def test_gateway_backend_uses_configured_timeout(mocker, monkeypatch):
    """The gateway OCR call must use DOCUMENT_OCR_TIMEOUT_SECONDS (resolved per
    call), not the old hardcoded 180s constant."""
    resp = mocker.Mock()
    resp.raise_for_status = mocker.Mock()
    resp.json = mocker.Mock(return_value={"pages": [{"index": 0, "markdown": "ok"}]})

    client = mocker.MagicMock()
    client.__aenter__ = mocker.AsyncMock(return_value=client)
    client.__aexit__ = mocker.AsyncMock(return_value=False)
    client.post = mocker.AsyncMock(return_value=resp)

    captured: dict[str, Any] = {}

    def _make_client(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return client

    monkeypatch.setattr(ocr.httpx, "AsyncClient", _make_client)
    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_timeout_seconds=42.0)
    )

    backend = ocr._GatewayOcrBackend("https://gw", "mistral/mistral-ocr-latest")
    await backend.ocr(b"%PDF-1.7", "application/pdf")

    # httpx.Timeout(42.0, connect=10.0): the read/overall budget is the setting.
    assert captured["timeout"].read == pytest.approx(42.0)
    assert captured["timeout"].connect == pytest.approx(10.0)


async def test_mistral_backend_applies_timeout(mocker, monkeypatch):
    """The Mistral backend wraps process_async in DOCUMENT_OCR_TIMEOUT_SECONDS,
    so a slow OCR call fails fast instead of hanging on the SDK default."""
    monkeypatch.setattr(
        ocr, "get_settings", lambda: _settings(document_ocr_timeout_seconds=0.01)
    )

    # Bypass the SDK constructor; only the two attributes ocr() reads matter.
    backend = ocr._MistralOcrBackend.__new__(ocr._MistralOcrBackend)
    backend._model = "mistral-ocr-latest"

    async def _slow(*args, **kwargs):
        await anyio.sleep(1.0)

    backend._client = mocker.MagicMock()
    backend._client.ocr.process_async = _slow

    with pytest.raises(TimeoutError):
        await backend.ocr(b"%PDF-1.7", "application/pdf")


# --- batch mode (Deck #332) --------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        None,
        {},
        {"doc_id": "d", "doc_type": "file"},  # missing user_id
        {"user_id": "u", "doc_type": "file"},  # missing doc_id
        {"user_id": "u", "doc_id": "d"},  # missing doc_type
        {"user_id": "u", "doc_id": "d", "doc_type": ""},  # empty doc_type
    ],
)
def test_batch_identity_returns_none_without_full_identity(options):
    assert ocr._batch_identity(options) is None


def test_batch_identity_extracts_tuple_and_defaults_etag():
    assert ocr._batch_identity(
        {"user_id": "u", "doc_id": "d", "doc_type": "file", "etag": "v1"}
    ) == ("u", "d", "file", "v1")
    # etag may be absent/empty -> normalised to "".
    assert ocr._batch_identity({"user_id": "u", "doc_id": "d", "doc_type": "file"}) == (
        "u",
        "d",
        "file",
        "",
    )


from nextcloud_mcp_server.embedding.gateway_batch_client import (  # noqa: E402
    BatchPollResult,
)
from nextcloud_mcp_server.vector import batch_ocr_store as _bos  # noqa: E402

_IDENTITY = {"user_id": "u1", "doc_id": "d1", "doc_type": "file", "etag": "v1"}


class _FakeStore:
    """In-memory stand-in for BatchOcrJobStore keyed like the real table."""

    def __init__(self, preset=None):
        self.rows: dict[tuple, Any] = {}
        self.deleted: list[tuple] = []
        self.stale_swept: list[tuple] = []
        if preset is not None:
            self.rows[("u1", "d1", "file", "v1")] = preset

    async def get(self, *, user_id, doc_id, doc_type, etag):
        return self.rows.get((user_id, doc_id, doc_type, etag))

    async def insert_pending(
        self, *, user_id, doc_id, doc_type, etag, job_id, submitted_at=None
    ):
        self.rows[(user_id, doc_id, doc_type, etag)] = SimpleNamespace(
            job_id=job_id, status="pending", submitted_at=submitted_at or 1000
        )

    async def delete(self, *, user_id, doc_id, doc_type, etag):
        self.deleted.append((user_id, doc_id, doc_type, etag))
        self.rows.pop((user_id, doc_id, doc_type, etag), None)

    async def delete_stale_for_doc(self, *, user_id, doc_id, doc_type, keep_etag):
        self.stale_swept.append((user_id, doc_id, doc_type, keep_etag))


class _FakeBatchClient:
    def __init__(self, *, submit_job="mistral/job-1", poll=None):
        self._submit_job = submit_job
        self._poll = poll or BatchPollResult(status="pending", pages=[])
        self.submitted: list[tuple] = []
        self.polled: list[str] = []

    async def submit(self, content, mime_type, custom_id):
        self.submitted.append((content, mime_type, custom_id))
        return self._submit_job

    async def poll(self, job_id):
        self.polled.append(job_id)
        return self._poll


def _wire_batch(monkeypatch, *, client, store, settings=None):
    settings = settings or _settings(
        document_ocr_mode="batch",
        document_ocr_provider="gateway",
        embedding_gateway_url="https://gw",
    )
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    monkeypatch.setattr(ocr, "build_gateway_batch_client", lambda s: client)

    async def _shared(cls):
        return store

    monkeypatch.setattr(_bos.BatchOcrJobStore, "shared", classmethod(_shared))


async def test_batch_first_run_submits_and_returns_pending_sentinel(monkeypatch):
    client = _FakeBatchClient()
    store = _FakeStore()
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF-1.7", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is False
    assert r.metadata[ocr.OCR_BATCH_PENDING_KEY] is True
    assert r.metadata[ocr.OCR_BATCH_RETRY_IN_KEY] == 120
    # submitted with the doc id as custom_id, recorded a pending row, swept stale
    assert client.submitted and client.submitted[0][2] == "d1"
    assert store.rows[("u1", "d1", "file", "v1")].job_id == "mistral/job-1"
    assert store.stale_swept == [("u1", "d1", "file", "v1")]


async def test_batch_existing_pending_polls_and_defers(monkeypatch):
    preset = SimpleNamespace(job_id="mistral/j", status="pending", submitted_at=1000)
    client = _FakeBatchClient(poll=BatchPollResult(status="pending", pages=[]))
    store = _FakeStore(preset=preset)
    # submitted just now -> deadline not reached
    monkeypatch.setattr(ocr.time, "time", lambda: 1000.0)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert client.polled == ["mistral/j"]
    assert r.metadata[ocr.OCR_BATCH_PENDING_KEY] is True
    assert client.submitted == []  # did NOT resubmit


async def test_batch_succeeded_returns_indexed_result(monkeypatch):
    preset = SimpleNamespace(job_id="mistral/j", status="pending", submitted_at=1000)
    client = _FakeBatchClient(
        poll=BatchPollResult(status="succeeded", pages=[(0, "# One"), (1, "## Two")])
    )
    store = _FakeStore(preset=preset)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is True
    assert r.text == "# One\n\n## Two"
    assert r.metadata["page_count"] == 2
    assert ("u1", "d1", "file", "v1") in store.deleted  # row cleaned up


async def test_batch_failed_marks_parse_error(monkeypatch):
    preset = SimpleNamespace(job_id="mistral/j", status="pending", submitted_at=1000)
    client = _FakeBatchClient(
        poll=BatchPollResult(status="failed", pages=[], error="x")
    )
    store = _FakeStore(preset=preset)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "error"
    assert ("u1", "d1", "file", "v1") in store.deleted


async def test_batch_deadline_exceeded_marks_timeout(monkeypatch):
    preset = SimpleNamespace(job_id="mistral/j", status="pending", submitted_at=1000)
    client = _FakeBatchClient(poll=BatchPollResult(status="pending", pages=[]))
    store = _FakeStore(preset=preset)
    # now far past submitted_at + max_wait (86400)
    monkeypatch.setattr(ocr.time, "time", lambda: 1000.0 + 90000)
    _wire_batch(monkeypatch, client=client, store=store)

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )

    assert r.success is False
    assert r.metadata["parse_failed_reason"] == "timeout"
    assert ("u1", "d1", "file", "v1") in store.deleted


async def test_batch_falls_back_to_sync_when_no_gateway(monkeypatch):
    class _FakeBackend:
        async def ocr(self, content, mime_type):
            return "sync text", [{"page": 1, "start_offset": 0, "end_offset": 9}]

    settings = _settings(document_ocr_mode="batch", document_ocr_provider="mistral")
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    monkeypatch.setattr(ocr, "build_gateway_batch_client", lambda s: None)
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s: _FakeBackend())

    r = await ocr.OcrProcessor().process(
        b"%PDF", "application/pdf", options=dict(_IDENTITY)
    )
    assert r.success is True and r.text == "sync text"


async def test_batch_falls_back_to_sync_when_no_identity(monkeypatch):
    class _FakeBackend:
        async def ocr(self, content, mime_type):
            return "sync text", [{"page": 1, "start_offset": 0, "end_offset": 9}]

    client = _FakeBatchClient()
    settings = _settings(
        document_ocr_mode="batch",
        document_ocr_provider="gateway",
        embedding_gateway_url="https://gw",
    )
    monkeypatch.setattr(ocr, "get_settings", lambda: settings)
    monkeypatch.setattr(ocr, "build_gateway_batch_client", lambda s: client)
    monkeypatch.setattr(ocr, "build_ocr_backend", lambda s: _FakeBackend())

    # No options -> inline path -> batch inapplicable -> sync fallback.
    r = await ocr.OcrProcessor().process(b"%PDF", "application/pdf", options=None)
    assert r.success is True and r.text == "sync text"
    assert client.submitted == []  # never attempted batch
