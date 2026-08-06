"""Spreadsheets are read cell-by-cell, keeping the row/column association."""

import io

import openpyxl
import pytest

from nextcloud_mcp_server.document_processors import _libreoffice
from nextcloud_mcp_server.document_processors.base import ProcessorError
from nextcloud_mcp_server.document_processors.spreadsheet import (
    SHEET_BOUNDARIES_KEY,
    XLS_MIME,
    XLSX_MIME,
    SpreadsheetProcessor,
)

pytestmark = pytest.mark.unit


def _workbook(sheets: dict[str, list[list]]) -> bytes:
    """An .xlsx containing ``{sheet name: rows}``."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestLegacyXls:
    """The .xls branch, mocked so it stays covered in the fast unit lane.

    The real conversion is exercised by
    tests/integration/test_office_documents.py, which needs a soffice binary and
    is skipped without one -- so without these the branch has no coverage at all
    on a machine or CI lane lacking LibreOffice.
    """

    async def test_it_converts_to_xlsx_then_reads_cells(self, mocker):
        converted = _workbook({"Answers": [["Domain", "Answer"], ["Certs", "ISO"]]})
        convert = mocker.patch.object(
            _libreoffice, "convert", mocker.AsyncMock(return_value=converted)
        )

        result = await SpreadsheetProcessor().process(
            b"legacy ole2 bytes", XLS_MIME, "s.xls"
        )

        # xlsx, never pdf: a PDF rendition loses a third of the cells.
        assert convert.await_args.args[2] == "xlsx"
        assert "| Certs | ISO |" in result.text
        assert result.metadata["converted_from_mime"] == XLS_MIME

    async def test_conversion_failure_becomes_a_processor_error(self, mocker):
        mocker.patch.object(
            _libreoffice,
            "convert",
            mocker.AsyncMock(side_effect=_libreoffice.LibreOfficeError("no filter")),
        )

        with pytest.raises(ProcessorError, match="Legacy spreadsheet conversion"):
            await SpreadsheetProcessor().process(b"x", XLS_MIME, "s.xls")

    async def test_xlsx_never_goes_near_libreoffice(self, mocker):
        convert = mocker.patch.object(_libreoffice, "convert", mocker.AsyncMock())

        await SpreadsheetProcessor().process(
            _workbook({"S": [["a"]]}), XLSX_MIME, "s.xlsx"
        )

        convert.assert_not_awaited()


async def test_rows_become_a_markdown_table():
    content = _workbook(
        {"Questions": [["Domain", "Answer"], ["Certifications", "ISO 27001"]]}
    )

    result = await SpreadsheetProcessor().process(content, XLSX_MIME, "q.xlsx")

    assert "## Questions" in result.text
    assert "| Domain | Answer |" in result.text
    assert "| Certifications | ISO 27001 |" in result.text
    assert result.metadata["sheet_count"] == 1
    assert result.metadata["parse_mode"] == "markdown"


async def test_each_sheet_gets_its_own_boundary_span():
    content = _workbook({"First": [["a"]], "Second": [["b"]]})

    result = await SpreadsheetProcessor().process(content, XLSX_MIME, "two.xlsx")

    spans = result.metadata[SHEET_BOUNDARIES_KEY]
    assert [s["sheet"] for s in spans] == ["First", "Second"]
    # Offsets must index the returned text exactly -- they are what attributes a
    # chunk back to a sheet, the spreadsheet stand-in for a page number.
    for span in spans:
        segment = result.text[span["start_offset"] : span["end_offset"]]
        assert segment.startswith(f"## {span['sheet']}")
    assert spans[0]["end_offset"] == spans[1]["start_offset"]


async def test_pipe_in_a_cell_does_not_break_the_row():
    """An unescaped | would end the column early and shift later values."""
    content = _workbook({"S": [["head", "other"], ["a|b", "kept"]]})

    result = await SpreadsheetProcessor().process(content, XLSX_MIME, "p.xlsx")

    assert r"| a\|b | kept |" in result.text


async def test_newline_in_a_cell_does_not_break_the_table():
    content = _workbook({"S": [["head"], ["line one\nline two"]]})

    result = await SpreadsheetProcessor().process(content, XLSX_MIME, "n.xlsx")

    assert "| line one line two |" in result.text


async def test_ragged_rows_are_padded_to_the_widest():
    """A short row must not shift its cells under the wrong heading."""
    content = _workbook({"S": [["a", "b", "c"], ["only"]]})

    result = await SpreadsheetProcessor().process(content, XLSX_MIME, "r.xlsx")

    assert "| only |  |  |" in result.text


async def test_empty_sheet_is_skipped_not_emitted_as_an_empty_table():
    content = _workbook({"Empty": [], "Real": [["x"]]})

    result = await SpreadsheetProcessor().process(content, XLSX_MIME, "e.xlsx")

    assert "## Empty" not in result.text
    assert "## Real" in result.text
    assert [s["sheet"] for s in result.metadata[SHEET_BOUNDARIES_KEY]] == ["Real"]
