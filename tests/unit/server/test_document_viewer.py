"""Unit tests for the MCP Apps document viewer tools.

What is pinned here is what a mistake would cost: redaction must really remove
the text, a save must never overwrite (the original or an earlier copy), a
model-supplied copy name must not escape the folder, and the model-described
review controls must stay inside their schema (the viewer renders them, so an
unknown field or action is refused at the boundary rather than trusted).
"""

from __future__ import annotations

import re

import pymupdf
import pytest
from mcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from pydantic import ValidationError

from nextcloud_mcp_server.server import document_viewer as dv

pytestmark = pytest.mark.unit


def _pdf(lines: list[str]) -> bytes:
    with pymupdf.open() as doc:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + 18 * i), line, fontsize=11)
        return doc.tobytes()


def _text(pdf: bytes) -> str:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return "".join(p.get_text() for p in doc)


PDF = _pdf(["Name: Jane Doe", "Address: 1 Example Street", "Notes: jane doe called"])


# --- text search and redaction -------------------------------------------------


def test_find_text_is_case_insensitive_and_normalised():
    hits = dv._find_text(PDF, ["Jane Doe"])
    assert len(hits) == 2  # "Jane Doe" and "jane doe"
    for h in hits:
        assert h["page"] == 1 and h["term"] == "Jane Doe"
        assert all(0 <= v <= 1 for v in h["bbox"])


def test_redact_removes_the_text_not_just_covers_it():
    regions = [
        dv.Region(page=h["page"], bbox=h["bbox"])
        for h in dv._find_text(PDF, ["1 Example Street"])
    ]
    out = dv._redact(PDF, regions)
    assert "Example Street" not in _text(out)
    assert "Jane Doe" in _text(out)  # only the region is removed


def test_redact_rejects_a_page_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        dv._redact(PDF, [dv.Region(page=2, bbox=(0, 0, 1, 1))])


# --- copy naming ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["../escape.pdf", "a/../../b.pdf", "dir/", "a\\b.pdf", "  "]
)
def test_target_path_refuses_names_that_escape_or_are_empty(bad):
    with pytest.raises(ValueError):
        dv._target_path("/Docs/report.pdf", bad)


def test_target_path_puts_bare_names_beside_the_source_and_adds_pdf():
    assert dv._target_path("/Docs/report.pdf", "shared") == "/Docs/shared.pdf"
    assert dv._target_path("/Docs/report.pdf", "Out/x.PDF") == "Out/x.PDF"


def test_default_name_steps_through_versions():
    assert dv._default_name("/Docs/report.pdf") == "report-redacted.pdf"
    assert dv._default_name("/Docs/report.pdf", 3) == "report-redacted-v3.pdf"


# --- model-described review controls -------------------------------------------


def test_review_accepts_the_documented_shape():
    r = dv.Review(
        heading="By person",
        buttons=[{"label": "Keep only A", "set": "only", "groups": ["A"]}],
    )
    assert r.buttons[0].set == "only"


@pytest.mark.parametrize(
    "review",
    [
        {
            "buttons": [{"label": "Save now", "set": "save", "groups": ["A"]}]
        },  # unknown action
        {
            "buttons": [{"label": "x", "set": "on", "groups": ["A"], "onclick": "y"}]
        },  # extra field
        {"script": "x"},  # extra top-level field
        {"heading": "h" * 61},
        {"buttons": [{"label": "l" * 41, "set": "on", "groups": ["A"]}]},
        {"buttons": [{"label": "x", "set": "on", "groups": []}]},
        {
            "buttons": [
                {"label": str(i), "set": "on", "groups": ["A"]} for i in range(7)
            ]
        },
    ],
)
def test_review_refuses_anything_outside_the_schema(review):
    with pytest.raises(ValidationError):
        dv.Review.model_validate(review)


# --- open_document -------------------------------------------------------------


@pytest.fixture
def stub(mocker):
    client = mocker.MagicMock()
    client.webdav.find_files_by_ids = mocker.AsyncMock(
        return_value=[{"path": "/Docs/report.pdf", "content_type": "application/pdf"}]
    )
    client.webdav.read_file = mocker.AsyncMock(
        return_value=(PDF, "application/pdf", "e1")
    )
    client.webdav.get_file_info = mocker.AsyncMock(return_value={"etag": '"e1"'})
    mocker.patch.object(dv, "get_client", mocker.AsyncMock(return_value=client))
    point = mocker.MagicMock(
        payload={
            "chunk_index": 0,
            "page_number": 1,
            "page_count": 1,
            "excerpt": "Name: Jane Doe",
            "chunk_bbox": [],
        }
    )
    qdrant = mocker.MagicMock()
    qdrant.scroll = mocker.AsyncMock(return_value=([point, point], None))
    mocker.patch.object(dv, "get_qdrant_client", mocker.AsyncMock(return_value=qdrant))
    mocker.patch.object(dv, "get_settings", return_value=mocker.MagicMock())
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


async def test_open_document_stages_grouped_terms_and_reports_the_rest(stub, mocker):
    res = await dv.open_document(
        ctx=mocker.MagicMock(),
        file_id=7,
        # Calling .fn directly skips argument validation, so pass models as the framework would.
        redact_text=[
            dv.RedactTerm(text="Jane Doe", group="Person A"),
            "1 Example Street",
            "ab",  # too short
            "Nowhere Lane",  # not in the document
        ],
        review=dv.Review(
            heading="By person",
            buttons=[{"label": "Only A", "set": "only", "groups": ["Person A"]}],
        ),
    )
    s = res.structured_content
    assert {(r["term"], r.get("group")) for r in s["staged_regions"]} == {
        ("Jane Doe", "Person A"),
        ("1 Example Street", None),
    }
    assert s["review"]["buttons"][0]["groups"] == ["Person A"]
    assert len(s["chunks"]) == 1  # duplicate points per chunk collapse to one
    text = res.content[0].text
    assert "Skipped (shorter than 3 characters): 'ab'" in text
    assert "Not found in the document text" in text and "'Nowhere Lane'" in text


async def test_open_document_does_not_stage_a_term_that_matches_too_much(stub, mocker):
    stub.webdav.read_file.return_value = (_pdf(["the"] * 40), "application/pdf", "e1")
    mocker.patch.object(dv, "MAX_TERM_REGIONS", 30)
    res = await dv.open_document(ctx=mocker.MagicMock(), file_id=7, redact_text=["the"])
    assert res.structured_content["staged_regions"] == []
    assert "too broad" in res.content[0].text


async def test_open_document_without_terms_does_not_download_the_file(stub, mocker):
    res = await dv.open_document(ctx=mocker.MagicMock(), file_id=7)
    stub.webdav.read_file.assert_not_called()
    assert (
        res.structured_content["etag"] == "e1"
        and res.structured_content["review"] is None
    )


# --- save_redaction ------------------------------------------------------------


def _accepted(name: str = "") -> AcceptedElicitation:
    return AcceptedElicitation(data=dv.SaveConfirmation(confirm=True, name=name))


async def _save(mocker, **kw):
    regions = [dv.Region(page=1, bbox=(0.1, 0.1, 0.5, 0.15))]
    return await dv.save_redaction(
        ctx=mocker.MagicMock(), file_id=7, regions=regions, etag='"e1"', **kw
    )


async def test_save_steps_past_existing_copies_and_never_overwrites(stub, mocker):
    stub.webdav.write_file = mocker.AsyncMock(
        side_effect=[{"status_code": 412}, {"status_code": 201}]
    )
    stub.webdav.get_fileid = mocker.AsyncMock(return_value="99")
    res = await _save(mocker, confirmation=_accepted())
    paths = [c.args[0] for c in stub.webdav.write_file.call_args_list]
    assert paths == ["/Docs/report-redacted.pdf", "/Docs/report-redacted-v2.pdf"]
    assert "/Docs/report.pdf" not in paths  # the original is never a write target
    assert res.structured_content["file_id"] == 99


async def test_save_with_an_explicit_name_that_exists_fails_instead_of_renaming(
    stub, mocker
):
    stub.webdav.write_file = mocker.AsyncMock(return_value={"status_code": 412})
    res = await _save(mocker, confirmation=_accepted("mine.pdf"))
    assert res.is_error and "already exists" in res.content[0].text
    stub.webdav.write_file.assert_called_once()


async def test_save_refuses_when_the_file_changed_since_review(stub, mocker):
    stub.webdav.read_file.return_value = (PDF, "application/pdf", "e2")
    stub.webdav.write_file = mocker.AsyncMock()
    res = await _save(mocker, confirmation=_accepted())
    assert res.is_error and "conflict" in res.content[0].text
    stub.webdav.write_file.assert_not_called()


async def test_save_does_nothing_when_the_user_declines(stub, mocker):
    stub.webdav.write_file = mocker.AsyncMock()
    res = await _save(mocker, confirmation=DeclinedElicitation())
    assert res.is_error
    stub.webdav.write_file.assert_not_called()


# --- served resource -----------------------------------------------------------


def test_every_external_script_is_pinned_with_sri():
    """Bumping a pdf.js URL without its hash must fail here, not in a browser."""
    tags = re.findall(r"<script\b[^>]*\bsrc=[^>]*>", dv._viewer_html())
    assert len(tags) == 2
    for tag in tags:
        assert 'src="https://cdnjs.cloudflare.com/' in tag
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/]{64}"', tag), tag
        assert 'crossorigin="anonymous"' in tag
