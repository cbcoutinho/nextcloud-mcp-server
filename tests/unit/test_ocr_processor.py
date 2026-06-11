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
    assert "timed out" in r.error


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
    assert "timed out" in r.error


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
