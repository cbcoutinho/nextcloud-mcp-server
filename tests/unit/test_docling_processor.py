"""Unit tests for the docling-serve client + DoclingProcessor (mocked HTTP)."""

import httpx
import pytest

from nextcloud_mcp_server.document_processors import docling_serve
from nextcloud_mcp_server.document_processors.base import (
    DocumentProcessor,
    ProcessingResult,
    ProcessorError,
)
from nextcloud_mcp_server.document_processors.docling_serve import (
    DoclingProcessor,
    docling_pages,
)
from nextcloud_mcp_server.document_processors.registry import ProcessorRegistry

pytestmark = pytest.mark.unit


def _mock_client(mocker, *, json=None, status_code=200, raise_http=None):
    """A monkeypatchable httpx.AsyncClient stand-in returning one canned response."""
    resp = mocker.Mock()
    resp.status_code = status_code
    resp.json = mocker.Mock(return_value=json or {})
    if raise_http is not None:
        resp.raise_for_status = mocker.Mock(side_effect=raise_http)
    else:
        resp.raise_for_status = mocker.Mock()

    client = mocker.MagicMock()
    client.__aenter__ = mocker.AsyncMock(return_value=client)
    client.__aexit__ = mocker.AsyncMock(return_value=False)
    client.post = mocker.AsyncMock(return_value=resp)
    client.get = mocker.AsyncMock(return_value=resp)
    return client


# --- docling_pages -----------------------------------------------------------


def test_docling_pages_groups_by_page_no():
    json_content = {
        "texts": [
            {"text": "Alpha", "prov": [{"page_no": 1}]},
            {"text": "Bravo", "prov": [{"page_no": 1}]},
            {"text": "Charlie", "prov": [{"page_no": 2}]},
            {"text": "no-prov", "prov": []},  # dropped
            {"text": "", "prov": [{"page_no": 3}]},  # empty dropped
        ]
    }
    pages = docling_pages(json_content)
    # 0-based indices (ready for _pages_to_text), grouped + ordered by page_no.
    assert pages == [(0, "Alpha\n\nBravo"), (1, "Charlie")]


def test_docling_pages_empty_without_provenance():
    assert docling_pages({"texts": [{"text": "x"}]}) == []
    assert docling_pages(None) == []
    assert docling_pages({}) == []


# --- convert_file ------------------------------------------------------------


async def test_convert_file_sends_multipart_options(mocker, monkeypatch):
    client = _mock_client(
        mocker, json={"status": "success", "document": {"md_content": "# Hi\n\nbody"}}
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    document = await docling_serve.convert_file(
        "https://docling:5001/",
        b"\x89PNG...",
        "image/png",
        filename="scan.png",
        to_formats=["md"],
        do_ocr=True,
        ocr_lang=["en", "de"],
    )
    assert document["md_content"] == "# Hi\n\nbody"

    # URL is normalized (no double slash) and options are form fields.
    args, kwargs = client.post.call_args
    assert args[0] == "https://docling:5001/v1/convert/file"
    data = kwargs["data"]
    assert data["to_formats"] == ["md"]
    assert data["from_formats"] == ["image"]  # derived from image/png
    assert data["do_ocr"] == "true"
    assert data["ocr_lang"] == ["en", "de"]
    assert "files" in kwargs
    # Backward-compat guard: no VLM fields on the default (standard) path.
    assert "pipeline" not in data
    assert "vlm_pipeline_preset" not in data
    assert "image_export_mode" not in data


async def test_convert_file_pdf_from_format(mocker, monkeypatch):
    client = _mock_client(
        mocker, json={"status": "success", "document": {"text_content": "text"}}
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    await docling_serve.convert_file(
        "https://docling:5001",
        b"%PDF-1.7",
        "application/pdf",
        to_formats=["md", "json"],
        do_ocr=True,
    )
    _, kwargs = client.post.call_args
    assert kwargs["data"]["from_formats"] == ["pdf"]


async def test_convert_file_vlm_pipeline(mocker, monkeypatch):
    """pipeline='vlm' sends pipeline + preset + image_export_mode and OMITS the
    classic do_ocr/ocr_lang (inert in VLM)."""
    client = _mock_client(
        mocker, json={"status": "success", "document": {"md_content": "vlm text"}}
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    await docling_serve.convert_file(
        "https://docling:5001",
        b"\x89PNG",
        "image/png",
        to_formats=["md"],
        do_ocr=True,
        ocr_lang=["en", "de"],
        pipeline="vlm",
        vlm_pipeline_preset="glm_ocr",
    )
    data = client.post.call_args.kwargs["data"]
    assert data["pipeline"] == "vlm"
    assert data["vlm_pipeline_preset"] == "glm_ocr"
    assert data["image_export_mode"] == "placeholder"
    # classic-OCR fields are inert in VLM -> omitted
    assert "do_ocr" not in data
    assert "ocr_lang" not in data


async def test_convert_file_vlm_without_preset(mocker, monkeypatch):
    """pipeline='vlm' with no preset omits vlm_pipeline_preset (docling-serve picks
    its own default)."""
    client = _mock_client(
        mocker, json={"status": "success", "document": {"md_content": "vlm text"}}
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    await docling_serve.convert_file(
        "https://docling:5001",
        b"\x89PNG",
        "image/png",
        to_formats=["md"],
        pipeline="vlm",
    )
    data = client.post.call_args.kwargs["data"]
    assert data["pipeline"] == "vlm"
    assert "vlm_pipeline_preset" not in data


async def test_convert_file_http_error_raises_processor_error(mocker, monkeypatch):
    err = httpx.HTTPStatusError("500", request=mocker.Mock(), response=mocker.Mock())
    client = _mock_client(mocker, raise_http=err)
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    with pytest.raises(ProcessorError):
        await docling_serve.convert_file(
            "https://docling:5001", b"x", "image/png", to_formats=["md"]
        )


async def test_convert_file_failure_status_raises(mocker, monkeypatch):
    client = _mock_client(
        mocker, json={"status": "failure", "document": {}, "errors": ["boom"]}
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    with pytest.raises(ProcessorError):
        await docling_serve.convert_file(
            "https://docling:5001", b"x", "image/png", to_formats=["md"]
        )


async def test_convert_file_empty_text_raises(mocker, monkeypatch):
    client = _mock_client(
        mocker, json={"status": "success", "document": {"md_content": ""}}
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    with pytest.raises(ProcessorError):
        await docling_serve.convert_file(
            "https://docling:5001", b"x", "image/png", to_formats=["md"]
        )


async def test_convert_file_non_dict_body_raises(mocker, monkeypatch):
    """A valid-JSON but non-object body (bare list from a misbehaving proxy) must
    become a ProcessorError, not an AttributeError on body.get(...)."""
    client = _mock_client(mocker, json=["unexpected"])
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    with pytest.raises(ProcessorError):
        await docling_serve.convert_file(
            "https://docling:5001", b"x", "image/png", to_formats=["md"]
        )


async def test_convert_file_non_json_body_raises(mocker, monkeypatch):
    """A non-JSON body (response.json() raises ValueError) must become a
    ProcessorError, honoring the single-failure-type contract."""
    resp = mocker.Mock()
    resp.raise_for_status = mocker.Mock()
    resp.json = mocker.Mock(side_effect=ValueError("Expecting value"))
    client = mocker.MagicMock()
    client.__aenter__ = mocker.AsyncMock(return_value=client)
    client.__aexit__ = mocker.AsyncMock(return_value=False)
    client.post = mocker.AsyncMock(return_value=resp)
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    with pytest.raises(ProcessorError):
        await docling_serve.convert_file(
            "https://docling:5001", b"x", "image/png", to_formats=["md"]
        )


# --- DoclingProcessor --------------------------------------------------------


def test_supported_mime_types_images_only():
    proc = DoclingProcessor("https://docling:5001")
    assert "image/png" in proc.supported_mime_types
    # PDFs are deliberately excluded from auto-selection (handled by the OCR
    # backend or an explicit force_processor), so it never hijacks PDF tiering.
    assert "application/pdf" not in proc.supported_mime_types
    assert proc.name == "docling"
    assert proc.tier == "fast"


async def test_process_image_returns_markdown(mocker, monkeypatch):
    client = _mock_client(
        mocker,
        json={"status": "success", "document": {"md_content": "handwritten text"}},
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    proc = DoclingProcessor("https://docling:5001", ocr_lang=["en", "de"])
    result = await proc.process(b"\x89PNG", "image/png", filename="note.png")
    assert isinstance(result, ProcessingResult)
    assert result.processor == "docling"
    assert result.text == "handwritten text"
    assert result.metadata["parsing_method"] == "docling"
    # Default (standard) pipeline is recorded in metadata; no VLM fields sent.
    assert result.metadata["docling_pipeline"] == "standard"
    data = client.post.call_args.kwargs["data"]
    assert "pipeline" not in data


async def test_process_image_vlm_pipeline(mocker, monkeypatch):
    """A DoclingProcessor built with pipeline='vlm' forwards the VLM fields to
    docling-serve and records the pipeline in parsing_metadata."""
    client = _mock_client(
        mocker,
        json={"status": "success", "document": {"md_content": "vlm markdown"}},
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    proc = DoclingProcessor(
        "https://docling:5001", pipeline="vlm", vlm_preset="glm_ocr"
    )
    result = await proc.process(b"\x89PNG", "image/png", filename="note.png")
    assert result.text == "vlm markdown"
    assert result.metadata["docling_pipeline"] == "vlm"
    # parsing_method stays "docling" -- pipeline is a sub-detail (D5).
    assert result.metadata["parsing_method"] == "docling"
    data = client.post.call_args.kwargs["data"]
    assert data["pipeline"] == "vlm"
    assert data["vlm_pipeline_preset"] == "glm_ocr"
    assert data["image_export_mode"] == "placeholder"
    assert "do_ocr" not in data


async def test_process_pdf_when_forced(mocker, monkeypatch):
    """process() handles a PDF (from_formats=pdf) even though PDFs aren't in
    supported_mime_types -- this is the forced-processor override path."""
    client = _mock_client(
        mocker,
        json={"status": "success", "document": {"md_content": "| a | b |\n| 1 | 2 |"}},
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    proc = DoclingProcessor("https://docling:5001")
    result = await proc.process(b"%PDF-1.7", "application/pdf", filename="tables.pdf")
    assert "| a | b |" in result.text
    _, kwargs = client.post.call_args
    assert kwargs["data"]["from_formats"] == ["pdf"]


async def test_process_with_progress_callback_returns_text(mocker, monkeypatch):
    """The concurrent progress-poller path (always taken via nc_webdav_read_file,
    which passes ctx.report_progress) still returns the converted text."""
    client = _mock_client(
        mocker, json={"status": "success", "document": {"md_content": "ocr text"}}
    )
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    async def progress_cb(progress, total, message):
        pass

    proc = DoclingProcessor("https://docling:5001", progress_interval=1)
    result = await proc.process(
        b"\x89PNG", "image/png", filename="n.png", progress_callback=progress_cb
    )
    assert result.processor == "docling"
    assert result.text == "ocr text"


async def test_process_http_error_raises(mocker, monkeypatch):
    err = httpx.HTTPStatusError("500", request=mocker.Mock(), response=mocker.Mock())
    client = _mock_client(mocker, raise_http=err)
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)

    proc = DoclingProcessor("https://docling:5001")
    with pytest.raises(ProcessorError):
        await proc.process(b"x", "image/png")


async def test_health_check_ok(mocker, monkeypatch):
    ok = _mock_client(mocker, status_code=200)
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: ok)
    assert await DoclingProcessor("https://docling:5001").health_check() is True


async def test_health_check_error_is_false(mocker, monkeypatch):
    client = mocker.MagicMock()
    client.__aenter__ = mocker.AsyncMock(return_value=client)
    client.__aexit__ = mocker.AsyncMock(return_value=False)
    client.get = mocker.AsyncMock(side_effect=httpx.ConnectError("down"))
    monkeypatch.setattr(docling_serve.httpx, "AsyncClient", lambda *a, **k: client)
    assert await DoclingProcessor("https://docling:5001").health_check() is False


# --- registry routing --------------------------------------------------------


class _FakeImageProc(DocumentProcessor):
    """A lower-priority processor that also handles images (like unstructured)."""

    @property
    def name(self) -> str:
        return "fake-images"

    @property
    def supported_mime_types(self) -> set[str]:
        return {"image/png", "application/pdf"}

    async def process(
        self, content, content_type, filename=None, options=None, progress_callback=None
    ):
        return ProcessingResult(text="fake", metadata={}, processor=self.name)

    async def health_check(self) -> bool:
        return True


def test_docling_wins_image_routing_but_not_pdf():
    registry = ProcessorRegistry()
    registry.register(_FakeImageProc(), priority=10)  # unstructured-like
    registry.register(DoclingProcessor("https://docling:5001"), priority=20)

    # Images route to docling (higher priority).
    assert registry.find_processor("image/png").name == "docling"
    # PDFs are unaffected -- docling is images-only, so the fake still wins there,
    # and docling never appears as a PDF tier candidate for any tier.
    assert registry.find_processor("application/pdf").name == "fake-images"
    for t in ("fast", "structured", "ocr"):
        picked = registry._pdf_processor_for_tier(t)
        assert picked is None or picked.name != "docling"


def test_docling_force_selectable_by_name():
    registry = ProcessorRegistry()
    registry.register(DoclingProcessor("https://docling:5001"), priority=20)
    # get_processor("docling") is what the forced-processor path resolves; it must
    # be found even for a PDF (supports() is bypassed on the forced path).
    assert registry.get_processor("docling") is not None


async def test_parse_document_threads_processor_name(monkeypatch):
    """parse_document forwards processor_name to registry.process -- the wire that
    lets nc_webdav_read_file(force_processor="docling") reach the forced path."""
    from nextcloud_mcp_server.utils import document_parser

    captured: dict = {}

    async def _fake_process(**kwargs):
        captured.update(kwargs)
        return ProcessingResult(text="ok", metadata={}, processor="docling")

    monkeypatch.setattr(
        document_parser, "get_document_processor_config", lambda: {"enabled": True}
    )
    monkeypatch.setattr(document_parser.get_registry(), "process", _fake_process)

    text, meta = await document_parser.parse_document(
        b"x", "application/pdf", "f.pdf", processor_name="docling"
    )
    assert captured["processor_name"] == "docling"
    assert text == "ok"
