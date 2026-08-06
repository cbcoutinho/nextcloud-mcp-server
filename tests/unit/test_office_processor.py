"""Word-processor documents route through a rendition at the structured tier."""

import pytest

from nextcloud_mcp_server import config
from nextcloud_mcp_server.document_processors import _libreoffice
from nextcloud_mcp_server.document_processors.base import (
    ProcessingResult,
    ProcessorError,
)
from nextcloud_mcp_server.document_processors.office import (
    RENDERED_FROM_KEY,
    OfficeDocumentProcessor,
    _default_name,
)

pytestmark = pytest.mark.unit

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC_MIME = "application/msword"


def _patch_convert(mocker, **kwargs):
    return mocker.patch.object(
        _libreoffice, "convert", mocker.AsyncMock(return_value=b"%PDF-1.7", **kwargs)
    )


def _patch_pdf(mocker, processor):
    """Stub the delegated PDF parse; its own behaviour is tested elsewhere."""
    return mocker.patch.object(
        processor,
        "_pdf",
        **{
            "process": mocker.AsyncMock(
                return_value=ProcessingResult(
                    text="# Contract\n\n| a | b |",
                    metadata={"page_boundaries": [{"page": 1}]},
                    processor="pymupdf",
                    success=True,
                )
            )
        },
    )


def test_the_delegate_is_the_structured_tier():
    """Not the classifier's choice: fast extracts zero tables from a rendition."""
    processor = OfficeDocumentProcessor()

    assert processor.tier == "structured"
    assert processor._pdf.tier == "structured"


async def test_rendition_result_is_stamped_with_the_source_format(mocker):
    """Downstream must know the geometry belongs to a derived PDF."""
    processor = OfficeDocumentProcessor()
    _patch_convert(mocker)
    _patch_pdf(mocker, processor)

    result = await processor.process(b"docx bytes", DOCX_MIME, "contract.docx")

    assert result.metadata[RENDERED_FROM_KEY] == DOCX_MIME
    assert result.metadata["page_boundaries"] == [{"page": 1}]
    assert result.processor == "office"


async def test_the_pdf_parse_receives_the_rendition_not_the_source(mocker):
    processor = OfficeDocumentProcessor()
    convert = _patch_convert(mocker)
    pdf = _patch_pdf(mocker, processor)

    await processor.process(b"docx bytes", DOCX_MIME, "contract.docx")

    convert.assert_awaited_once()
    assert convert.await_args.args[0] == b"docx bytes"
    assert convert.await_args.args[2] == "pdf"
    assert pdf.process.await_args.args[0] == b"%PDF-1.7"
    assert pdf.process.await_args.args[1] == "application/pdf"


async def test_an_oversize_rendition_is_rejected_before_parsing(mocker):
    """Rendering is not size-preserving, and the rendition skips the registry.

    A modest source can render far larger, and because the rendition goes
    straight to the PDF engine it would otherwise never meet the size cap an
    uploaded PDF has to pass.
    """
    processor = OfficeDocumentProcessor()
    mocker.patch.object(
        _libreoffice, "convert", mocker.AsyncMock(return_value=b"%PDF-" + b"x" * 4096)
    )
    pdf = _patch_pdf(mocker, processor)
    mocker.patch.object(
        config, "get_settings", lambda: mocker.Mock(document_max_pdf_size_mb=0.001)
    )

    result = await processor.process(b"docx bytes", DOCX_MIME, "big.docx")

    assert result.success is False
    assert result.metadata["parse_failed_reason"] == "oversize"
    assert result.metadata[RENDERED_FROM_KEY] == DOCX_MIME
    pdf.process.assert_not_awaited()


async def test_a_rendition_within_the_cap_is_parsed(mocker):
    processor = OfficeDocumentProcessor()
    _patch_convert(mocker)
    pdf = _patch_pdf(mocker, processor)
    mocker.patch.object(
        config, "get_settings", lambda: mocker.Mock(document_max_pdf_size_mb=100)
    )

    result = await processor.process(b"docx bytes", DOCX_MIME, "ok.docx")

    assert result.success is True
    pdf.process.assert_awaited_once()


async def test_conversion_failure_becomes_a_processor_error(mocker):
    processor = OfficeDocumentProcessor()
    mocker.patch.object(
        _libreoffice,
        "convert",
        mocker.AsyncMock(side_effect=_libreoffice.LibreOfficeError("exited 1")),
    )

    with pytest.raises(ProcessorError, match="Office rendition failed"):
        await processor.process(b"x", DOC_MIME, "c.doc")


def test_default_name_keeps_the_right_import_filter():
    """LibreOffice picks its filter from the extension, so it must be correct."""
    assert _default_name(DOC_MIME).endswith(".doc")
    assert _default_name(DOCX_MIME).endswith(".docx")
    assert _default_name("application/msword; charset=binary").endswith(".doc")
