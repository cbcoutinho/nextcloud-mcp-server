"""Unit tests for the tier-3 OCR processor + backend selection."""

from types import SimpleNamespace
from typing import Any

import pytest

from nextcloud_mcp_server.document_processors import ocr

pytestmark = pytest.mark.unit


def _settings(**kw) -> Any:  # a Settings stand-in (only the read fields matter)
    base = dict(
        document_ocr_provider="auto",
        document_ocr_model="mistral/mistral-ocr-latest",
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
