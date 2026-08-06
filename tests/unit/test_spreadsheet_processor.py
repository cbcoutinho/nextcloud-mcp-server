"""Spreadsheets are read cell-by-cell, keeping the row/column association."""

import io

import openpyxl
import pytest

from nextcloud_mcp_server.document_processors.spreadsheet import (
    SHEET_BOUNDARIES_KEY,
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
