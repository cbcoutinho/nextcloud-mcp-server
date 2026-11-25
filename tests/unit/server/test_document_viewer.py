"""Unit tests for the MCP Apps document viewer tools.

What is pinned here is what a mistake would cost: redaction must really remove
the text, a save must never overwrite (the original or an earlier copy), a
model-supplied copy name must not escape the folder, and the model-described
review controls must stay inside their schema (the viewer renders them, so an
unknown field or action is refused at the boundary rather than trusted).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

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


def _rotated_pdf(rotation: int) -> bytes:
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Name: Jane Doe", fontsize=11)
        page.insert_text((72, 700), "Keep this line", fontsize=11)
        page.set_rotation(rotation)
        return doc.tobytes()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_redaction_lands_on_the_text_of_a_rotated_page(rotation):
    src = _rotated_pdf(rotation)
    hits = dv._find_text(src, ["Jane Doe"])
    out = dv._redact(src, [dv.Region(page=1, bbox=h["bbox"]) for h in hits])
    assert "Jane Doe" not in _text(out)
    assert "Keep this line" in _text(out)


def test_staged_boxes_are_in_displayed_page_space():
    """At 180 degrees the line near the top is drawn near the bottom right."""
    (upright,) = dv._find_text(_rotated_pdf(0), ["Jane Doe"])
    (flipped,) = dv._find_text(_rotated_pdf(180), ["Jane Doe"])
    x0, y0, x1, y1 = upright["bbox"]
    assert flipped["bbox"] == pytest.approx([1 - x1, 1 - y1, 1 - x0, 1 - y0], abs=1e-6)


def test_a_box_drawn_on_a_rotated_page_redacts_what_the_user_saw():
    """A box the user draws arrives in displayed space, with no search behind it."""
    (upright,) = dv._find_text(_rotated_pdf(0), ["Jane Doe"])
    x0, y0, x1, y1 = upright["bbox"]
    drawn = dv.Region(page=1, bbox=(1 - x1, 1 - y1, 1 - x0, 1 - y0))
    out = dv._redact(_rotated_pdf(180), [drawn])
    assert "Jane Doe" not in _text(out)
    assert "Keep this line" in _text(out)


def test_redaction_blanks_the_pixels_of_a_scanned_page_under_the_box():
    """A scan is one page-sized image: the box must blank its pixels, not keep them."""
    red = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    red.set_rect(red.irect, (255, 0, 0))
    with pymupdf.open() as doc:
        page = doc.new_page(width=100, height=100)
        page.insert_image(page.rect, pixmap=red)
        scan = doc.tobytes()
    out = dv._redact(scan, [dv.Region(page=1, bbox=(0.4, 0.4, 0.6, 0.6))])
    with pymupdf.open(stream=out, filetype="pdf") as doc:
        (xref, *_), *_ = doc[0].get_images()
        img = pymupdf.Pixmap(doc, xref)
        sx, sy = img.width / 100, img.height / 100
        assert img.pixel(int(50 * sx), int(50 * sy)) != (255, 0, 0)  # under the box
        assert img.pixel(int(5 * sx), int(5 * sy)) == (255, 0, 0)  # rest of the page


def test_redaction_modes_match_pymupdf():
    assert dv.REDACT_IMAGE_PIXELS == pymupdf.PDF_REDACT_IMAGE_PIXELS
    assert (
        dv.REDACT_LINE_ART_IF_TOUCHED == pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED
    )
    assert dv.REDACT_TEXT_REMOVE == pymupdf.PDF_REDACT_TEXT_REMOVE


def test_redact_rejects_a_page_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        dv._redact(PDF, [dv.Region(page=2, bbox=(0, 0, 1, 1))])


def test_redacted_copy_drops_metadata_and_attachments():
    with pymupdf.open(stream=PDF, filetype="pdf") as doc:
        doc.set_metadata({"title": "Jane Doe", "author": "Jane Doe"})
        doc.embfile_add("notes.txt", b"Jane Doe")
        src = doc.tobytes()
    out = dv._redact(src, [dv.Region(page=1, bbox=(0.0, 0.0, 0.01, 0.01))])
    with pymupdf.open(stream=out, filetype="pdf") as doc:
        assert not any(doc.metadata.get(k) for k in ("title", "author"))
        assert doc.embfile_names() == []


@pytest.mark.parametrize(
    "bbox",
    [
        (0.5, 0.1, 0.4, 0.2),  # inverted x
        (0.1, 0.2, 0.5, 0.2),  # zero height
        (float("nan"), 0.1, 0.5, 0.2),
        (0.1, 0.1, float("inf"), 0.2),
        (1.2, 0.1, 1.5, 0.2),  # entirely off the page once clamped
    ],
)
def test_region_refuses_boxes_that_are_not_on_the_page(bbox):
    with pytest.raises(ValidationError):
        dv.Region(page=1, bbox=bbox)


def test_region_clamps_an_edge_overshoot_and_refuses_page_zero():
    assert dv.Region(page=1, bbox=(-0.01, 0.1, 1.02, 0.2)).bbox == (0.0, 0.1, 1.0, 0.2)
    with pytest.raises(ValidationError):
        dv.Region(page=0, bbox=(0.1, 0.1, 0.2, 0.2))


def test_redact_term_refuses_extra_fields_and_overlong_text():
    with pytest.raises(ValidationError):
        dv.RedactTerm.model_validate({"text": "Jane Doe", "colour": "red"})
    with pytest.raises(ValidationError):
        dv.RedactTerm(text="x" * (dv.MAX_TERM_CHARS + 1))


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


async def test_open_document_refuses_to_stage_redactions_in_a_non_pdf(stub, mocker):
    stub.webdav.find_files_by_ids.return_value = [
        {"path": "/Docs/notes.docx", "content_type": "application/msword"}
    ]
    with pytest.raises(ValueError, match="not a PDF"):
        await dv.open_document(
            ctx=mocker.MagicMock(), file_id=7, redact_text=["Jane Doe"]
        )
    stub.webdav.read_file.assert_not_called()


async def test_open_document_says_when_the_chunk_list_is_truncated(stub, mocker):
    mocker.patch.object(dv, "MAX_CHUNKS", 1)
    res = await dv.open_document(ctx=mocker.MagicMock(), file_id=7)
    assert "Only the first 1 chunks are listed." in res.content[0].text


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


async def test_save_refuses_when_the_file_version_cannot_be_checked(stub, mocker):
    stub.webdav.read_file.return_value = (PDF, "application/pdf", None)
    stub.webdav.write_file = mocker.AsyncMock()
    res = await _save(mocker, confirmation=_accepted())
    assert res.is_error and "conflict" in res.content[0].text
    stub.webdav.write_file.assert_not_called()


async def test_save_reports_a_failed_write_instead_of_succeeding(stub, mocker):
    stub.webdav.write_file = mocker.AsyncMock(
        return_value={"status_code": 423, "message": "file is locked"}
    )
    stub.webdav.get_fileid = mocker.AsyncMock()
    res = await _save(mocker, confirmation=_accepted())
    assert res.is_error and "locked" in res.content[0].text
    stub.webdav.get_fileid.assert_not_called()


async def test_accepting_the_suggested_name_steps_like_the_default(stub, mocker):
    stub.webdav.write_file = mocker.AsyncMock(
        side_effect=[{"status_code": 412}, {"status_code": 201}]
    )
    stub.webdav.get_fileid = mocker.AsyncMock(return_value="99")
    res = await _save(mocker, confirmation=_accepted("report-redacted.pdf"))
    assert not res.is_error
    assert res.structured_content["path"] == "/Docs/report-redacted-v2.pdf"


async def test_an_oversized_file_is_refused_before_downloading(stub, mocker):
    stub.webdav.find_files_by_ids.return_value = [
        {
            "path": "/Docs/report.pdf",
            "content_type": "application/pdf",
            "size": dv.MAX_BYTES + 1,
        }
    ]
    with pytest.raises(ValueError, match="cap"):
        await dv.read_document_bytes(ctx=mocker.MagicMock(), file_id=7)
    stub.webdav.read_file.assert_not_called()


async def test_read_document_bytes_refuses_a_non_pdf(stub, mocker):
    stub.webdav.find_files_by_ids.return_value = [
        {"path": "/Docs/notes.txt", "content_type": "text/plain"}
    ]
    with pytest.raises(ValueError, match="not a PDF"):
        await dv.read_document_bytes(ctx=mocker.MagicMock(), file_id=7)


async def test_save_reports_a_region_off_the_document_as_an_error(stub, mocker):
    stub.webdav.write_file = mocker.AsyncMock()
    res = await dv.save_redaction(
        ctx=mocker.MagicMock(),
        file_id=7,
        regions=[dv.Region(page=5, bbox=(0.1, 0.1, 0.5, 0.2))],
        etag='"e1"',
        confirmation=_accepted(),
    )
    assert res.is_error and "out of range" in res.content[0].text
    stub.webdav.write_file.assert_not_called()


async def test_save_refuses_a_non_pdf_as_a_tool_error(stub, mocker):
    stub.webdav.find_files_by_ids.return_value = [
        {"path": "/Docs/notes.txt", "content_type": "text/plain"}
    ]
    res = await _save(mocker, confirmation=_accepted())
    assert res.is_error and "not a PDF" in res.content[0].text


async def test_a_saved_copy_is_reported_even_if_its_id_lookup_fails(stub, mocker):
    stub.webdav.write_file = mocker.AsyncMock(return_value={"status_code": 201})
    stub.webdav.get_fileid = mocker.AsyncMock(side_effect=RuntimeError("boom"))
    res = await _save(mocker, confirmation=_accepted())
    assert not res.is_error
    assert res.structured_content["file_id"] is None
    assert res.structured_content["path"] == "/Docs/report-redacted.pdf"


def test_an_unreadable_pdf_is_a_value_error():
    with pytest.raises(ValueError, match="Could not read"):
        dv._find_text(b"not a pdf", ["Jane Doe"])


def test_a_password_protected_pdf_is_refused():
    with pymupdf.open(stream=PDF, filetype="pdf") as doc:
        locked = doc.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o"
        )
    with pytest.raises(ValueError, match="password"):
        dv._redact(locked, [dv.Region(page=1, bbox=(0.1, 0.1, 0.5, 0.2))])


async def test_save_turns_a_transport_failure_into_a_tool_error(stub, mocker):
    stub.webdav.write_file = mocker.AsyncMock(side_effect=RuntimeError("network"))
    res = await _save(mocker, confirmation=_accepted())
    assert res.is_error and "failed" in res.content[0].text


async def test_save_does_nothing_when_the_user_declines(stub, mocker):
    stub.webdav.write_file = mocker.AsyncMock()
    res = await _save(mocker, confirmation=DeclinedElicitation())
    assert res.is_error
    stub.webdav.write_file.assert_not_called()


# --- served resource -----------------------------------------------------------


def test_every_external_script_is_pinned_with_sri():
    """Bumping a pdf.js URL without its hash must fail here, not in a browser."""
    tags = re.findall(r"<script\b[^>]*\bsrc=[^>]*>", dv._VIEWER_FILE.read_text())
    assert len(tags) == 2
    for tag in tags:
        assert 'src="https://cdnjs.cloudflare.com/' in tag
        # The algorithms _SCRIPT_TAG accepts, each with its base64 digest length.
        assert re.search(
            r'integrity="(sha256-[A-Za-z0-9+/]{43}=|sha384-[A-Za-z0-9+/]{64}'
            r'|sha512-[A-Za-z0-9+/]{86}==)"',
            tag,
        ), tag
        assert 'crossorigin="anonymous"' in tag


def test_served_viewer_loads_no_external_script():
    """claude.ai ignores csp.resourceDomains, so pdf.js must arrive inline.

    Also fails when a pinned tag is bumped without replacing the vendored file.
    """
    html = dv._viewer_html()
    assert re.findall(r"<script\b[^>]*\bsrc=", html) == []
    assert html.count('<script type="module">/* pdf') == 2
    # an inlined file containing "</script" would end its block early
    assert html.count("<script") == html.count("</script>")


@pytest.mark.parametrize("alg", ["sha256", "sha384", "sha512"])
def test_a_module_tag_is_inlined_as_a_module_under_any_sri_algorithm(
    tmp_path, monkeypatch, alg
):
    """cdnjs's own SRI for pdf.js is sha512; a Renovate repin may switch algorithms."""
    (tmp_path / "x.min.mjs").write_bytes(b"globalThis.x = 1;")
    monkeypatch.setattr(dv, "_VENDOR_DIR", tmp_path)
    sri = dv.sri_digest(alg, b"globalThis.x = 1;")
    tag = (
        '<script type="module" src="https://cdnjs.cloudflare.com/ajax/libs/x/1.0.0/x.min.mjs"'
        f' integrity="{sri}" crossorigin="anonymous"></script>'
    )
    out = dv._inline_vendored_scripts(tag)
    assert out.startswith('<script type="module">') and "globalThis.x = 1;" in out


def test_sync_script_reads_pins_exactly_like_the_server():
    """scripts/sync_pdfjs.py is stdlib-only, so it carries its own copy of the tag regex."""
    spec = importlib.util.spec_from_file_location(
        "sync_pdfjs", Path(__file__).parents[3] / "scripts" / "sync_pdfjs.py"
    )
    assert spec and spec.loader
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    assert sync.SCRIPT_TAG.pattern == dv._SCRIPT_TAG.pattern
    assert sync.VIEWER_FILE == dv._VIEWER_FILE.resolve()
    assert sync.VENDOR_DIR == dv._VENDOR_DIR.resolve()


def test_a_vendored_file_that_misses_its_pin_is_not_inlined(tmp_path, monkeypatch):
    (tmp_path / "pdf.min.js").write_text("tampered")
    monkeypatch.setattr(dv, "_VENDOR_DIR", tmp_path)
    tag = (
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"'
        ' integrity="sha384-' + "A" * 64 + '" crossorigin="anonymous"></script>'
    )
    assert dv._inline_vendored_scripts(tag) == tag
    assert dv._inline_vendored_scripts(tag.replace("pdf.min", "absent")) == tag.replace(
        "pdf.min", "absent"
    )
