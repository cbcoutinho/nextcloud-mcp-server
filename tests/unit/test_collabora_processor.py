"""Legacy/ODF office files go through Collabora's convert-to, then a native
OOXML reader (ADR-039). coolwsd is replaced by an httpx.MockTransport; the
OOXML it "returns" is real, so the hand-off to the reader is exercised too."""

import io

import httpx
import pytest
from docx import Document

from nextcloud_mcp_server.document_processors import collabora
from nextcloud_mcp_server.document_processors.base import ProcessorError
from nextcloud_mcp_server.document_processors.collabora import (
    CONVERSIONS,
    CollaboraProcessor,
)
from nextcloud_mcp_server.document_processors.presentation import PptxProcessor
from nextcloud_mcp_server.document_processors.spreadsheet import XlsxProcessor
from nextcloud_mcp_server.document_processors.word import DocxProcessor

pytestmark = pytest.mark.unit

URL = "http://collabora:9980"
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
DOC_MIME = "application/msword"


def _docx(text: str) -> bytes:
    doc = Document()
    doc.add_heading(text, level=1)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _processor() -> CollaboraProcessor:
    return CollaboraProcessor(
        URL,
        readers={
            "docx": DocxProcessor(),
            "xlsx": XlsxProcessor(),
            "pptx": PptxProcessor(),
        },
    )


@pytest.fixture
def coolwsd(monkeypatch):
    """Route collabora's httpx clients to a handler; returns the request log."""
    requests: list[httpx.Request] = []
    state: dict = {"handler": lambda r: httpx.Response(200, content=_docx("Converted"))}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return state["handler"](request)

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(collabora.httpx, "AsyncClient", client)
    state["requests"] = requests
    return state


def test_claims_the_legacy_and_odf_types():
    assert _processor().supported_mime_types == set(CONVERSIONS)
    assert DOC_MIME in CONVERSIONS
    assert "application/vnd.oasis.opendocument.spreadsheet" in CONVERSIONS


async def test_doc_is_converted_to_docx_and_read_natively(coolwsd):
    result = await _processor().process(OLE2, DOC_MIME, "old.doc")

    assert result.text == "# Converted\n"
    assert result.processor == "collabora+word"
    assert result.metadata["converted_from_mime"] == DOC_MIME
    assert result.metadata["converted_by"] == "collabora"
    (request,) = coolwsd["requests"]
    assert request.method == "POST"
    assert str(request.url) == f"{URL}/cool/convert-to/docx"
    # coolwsd picks its import filter from the part's filename extension.
    body = request.content
    assert b'name="data"; filename="document.doc"' in body


async def test_odf_is_accepted_as_a_zip_container(coolwsd):
    result = await _processor().process(
        b"PK\x03\x04rest", "application/vnd.oasis.opendocument.text", "new.odt"
    )

    assert result.text == "# Converted\n"
    assert b'filename="document.odt"' in coolwsd["requests"][0].content


async def test_bytes_that_are_not_the_claimed_container_never_reach_coolwsd(coolwsd):
    """LibreOffice imports unrecognised bytes as plain text, so coolwsd would
    return a 200 'document' of garbage."""
    with pytest.raises(ProcessorError, match="not a doc container"):
        await _processor().process(b"plain text pretending", DOC_MIME, "junk.doc")

    assert coolwsd["requests"] == []


async def test_a_denied_client_names_the_allowlist(coolwsd):
    coolwsd["handler"] = lambda r: httpx.Response(403)

    with pytest.raises(ProcessorError, match="net.post_allow"):
        await _processor().process(OLE2, DOC_MIME, "old.doc")


async def test_a_rejected_file_raises(coolwsd):
    coolwsd["handler"] = lambda r: httpx.Response(500)

    with pytest.raises(ProcessorError, match="HTTP 500"):
        await _processor().process(OLE2, DOC_MIME, "old.doc")


async def test_an_empty_conversion_raises(coolwsd):
    coolwsd["handler"] = lambda r: httpx.Response(200, content=b"")

    with pytest.raises(ProcessorError, match="empty docx"):
        await _processor().process(OLE2, DOC_MIME, "old.doc")


async def test_an_unreachable_service_raises(coolwsd):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    coolwsd["handler"] = refuse

    with pytest.raises(ProcessorError, match="refused"):
        await _processor().process(OLE2, DOC_MIME, "old.doc")


@pytest.mark.parametrize(
    ("response", "healthy"),
    [
        (httpx.Response(200, json={"convert-to": {"available": True}}), True),
        # Reported available=false when this client is outside net.post_allow.
        (httpx.Response(200, json={"convert-to": {"available": False}}), False),
        (httpx.Response(200, content=b"not json"), False),
        (httpx.Response(503), False),
    ],
)
async def test_health_check_reads_the_capabilities_endpoint(coolwsd, response, healthy):
    coolwsd["handler"] = lambda r: response

    assert await _processor().health_check() is healthy
    assert str(coolwsd["requests"][0].url) == f"{URL}/hosting/capabilities"
