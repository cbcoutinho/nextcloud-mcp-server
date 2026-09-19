"""Spreadsheets are read cell-by-cell into one markdown table per sheet (ADR-038)."""

import io

import pytest
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlImage
from PIL import Image

from nextcloud_mcp_server.document_processors import _ooxml, spreadsheet
from nextcloud_mcp_server.document_processors._ooxml import PictureCaptioner
from nextcloud_mcp_server.document_processors.base import ProcessorError
from nextcloud_mcp_server.document_processors.spreadsheet import (
    XLSX_MIME,
    XlsxProcessor,
)

pytestmark = pytest.mark.unit


def _save(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _workbook(sheets: dict[str, list[list]]) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    return wb


def test_supported_mime_type_is_xlsx_only():
    assert XlsxProcessor().supported_mime_types == {XLSX_MIME}


async def test_each_sheet_becomes_a_markdown_table():
    wb = _workbook(
        {
            "Budget": [["Item", "Cost"], ["Rent", 1200], [None, None], ["Food", 300]],
            "Empty": [],
            "Notes": [["only", None, None]],
        }
    )

    result = await XlsxProcessor().process(_save(wb), XLSX_MIME, "b.xlsx")

    assert result.text == (
        "## Sheet: Budget\n\n| Item | Cost |\n| --- | --- |\n"
        "| Rent | 1200 |\n| Food | 300 |\n\n"
        "## Sheet: Notes\n\n| only |\n| --- |\n"
    )
    assert result.metadata["sheet_names"] == ["Budget", "Empty", "Notes"]
    assert result.metadata["sheet_count"] == 3
    assert result.metadata["pictures_found"] == 0


async def test_ragged_rows_are_padded_and_cells_escaped():
    wb = _workbook({"S": [["a", "b", "c"], ["x|y"], ["line one\nline two", 2]]})

    result = await XlsxProcessor().process(_save(wb), XLSX_MIME, "r.xlsx")

    assert "| x\\|y |  |  |" in result.text
    assert "| line one line two | 2 |  |" in result.text


async def test_formula_text_is_never_emitted():
    """data_only reads a formula's cached value; with none cached (as here,
    openpyxl never computes one) the cell is empty, not '=SUM(...)'."""
    wb = _workbook({"S": [["n", "total"], [1, "=SUM(A2:A2)"]]})

    result = await XlsxProcessor().process(_save(wb), XLSX_MIME, "f.xlsx")

    assert "=SUM" not in result.text
    assert "| 1 |" in result.text


async def test_workbook_inflating_past_the_cap_is_refused(monkeypatch):
    content = _save(_workbook({"S": [["x"]]}))
    monkeypatch.setattr(_ooxml, "MAX_UNCOMPRESSED_BYTES", 1)

    with pytest.raises(ProcessorError, match="uncompressed size"):
        await XlsxProcessor().process(content, XLSX_MIME, "bomb.xlsx")


def _workbook_with_pictures(*sizes: tuple[int, int]) -> bytes:
    wb = _workbook({"S": [["data"]]})
    for i, (width, height) in enumerate(sizes):
        png = io.BytesIO()
        Image.new("RGB", (width, height), (200, 50, 50)).save(png, "PNG")
        # openpyxl re-reads the image's own file object at save time, so hand it
        # encoded bytes rather than an in-memory PIL image.
        wb["S"].add_image(XlImage(io.BytesIO(png.getvalue())), f"C{i + 2}")
    return _save(wb)


async def test_picture_is_counted_but_not_captioned_when_disabled(mocker, monkeypatch):
    convert = mocker.AsyncMock()
    monkeypatch.setattr(_ooxml, "convert_file", convert)

    result = await XlsxProcessor().process(
        _workbook_with_pictures((120, 120), (40, 40)), XLSX_MIME, "p.xlsx"
    )

    convert.assert_not_called()
    assert result.metadata["pictures_found"] == 1
    assert "pictures_captioned" not in result.metadata
    assert "## Images" not in result.text


async def test_captions_go_in_a_trailing_images_section(mocker, monkeypatch):
    convert = mocker.AsyncMock(
        side_effect=[{"md_content": "a chart"}, ProcessorError("timeout")]
    )
    monkeypatch.setattr(_ooxml, "convert_file", convert)
    processor = XlsxProcessor(
        captioner=PictureCaptioner(
            caption_images=True, docling_api_url="https://docling:5001"
        )
    )

    result = await processor.process(
        _workbook_with_pictures((120, 120), (120, 120)), XLSX_MIME, "p.xlsx"
    )

    assert result.text.endswith("## Images\n\n*Image: a chart*\n")
    assert result.metadata["pictures_found"] == 2
    # One failed caption costs that picture only.
    assert result.metadata["pictures_captioned"] == 1
    args, _ = convert.call_args_list[0]
    assert args[2] == "image/png"


def test_media_is_ordered_naturally_so_the_cap_takes_the_first_pictures():
    names = ["xl/media/image10.png", "xl/media/image2.png", "xl/media/image1.png"]

    assert sorted(names, key=spreadsheet._natural_key) == [
        "xl/media/image1.png",
        "xl/media/image2.png",
        "xl/media/image10.png",
    ]


async def test_health_check_is_true_once_openpyxl_is_importable():
    assert await XlsxProcessor().health_check() is True
