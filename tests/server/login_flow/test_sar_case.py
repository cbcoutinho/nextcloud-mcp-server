"""End-to-end: SAR cases on the login-flow server (ADR-040).

Login flow is the deployment Astrolabe Cloud runs, and it is the one that
exercises the production paths: cases are read and written, and exports run,
with the user's stored app password (not env credentials), and the management
API Astrolabe calls, ``/api/v1/sar/cases``, exists only in authenticated modes.

The ``mcp-login-flow`` compose service points ``EMBEDDING_GATEWAY_URL`` at the
``ner-stub`` service (``tests/fixtures/ner_stub.py``), which "detects" a fixed
set of synthetic names. Each test indexes a document, runs a case through
create → items → export → ready for audit → close, and reads the archive back
from Nextcloud.
"""

import io
import json
import uuid
import zipfile
from collections.abc import Awaitable, Callable

import anyio
import httpx
import pymupdf
import pytest

from nextcloud_mcp_server.config import get_settings
from tests.integration._search_helpers import document_is_searchable

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]

CASES = "http://localhost:8004/api/v1/sar/cases"
INDEX_TIMEOUT_SECONDS = 180
EXPORT_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 3
LEAKS = ("Karen", "Smith", "Tom Brown", "karen@example.org", "Mill Lane", "XA9 8QT")


def _pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "".join(page.get_text() for page in doc)


async def _wait_indexed(mcp, term: str, note_id: int | None = None) -> None:
    with anyio.move_on_after(INDEX_TIMEOUT_SECONDS) as scope:
        while not await document_is_searchable(mcp, term, note_id=note_id):
            await anyio.sleep(POLL_INTERVAL_SECONDS)
    if scope.cancelled_caught:
        pytest.fail(f"document not indexed within {INDEX_TIMEOUT_SECONDS}s")


async def _wait_exported(get_case: Callable[[], Awaitable[dict]]) -> dict:
    with anyio.fail_after(EXPORT_TIMEOUT_SECONDS):
        while (data := await get_case())["case"]["state"] == "exporting":
            await anyio.sleep(POLL_INTERVAL_SECONDS)
    return data


async def _archive(nc_client, path: str) -> zipfile.ZipFile:
    content, _, _ = await nc_client.webdav.read_file(path)
    return zipfile.ZipFile(io.BytesIO(content))


def _tool_json(result) -> dict:
    assert result.is_error is False, result.content
    return json.loads(result.content[0].text)


@pytest.fixture
async def workspace(nc_client):
    """A fresh parent folder and a note naming the subject and third parties."""
    term = f"zorblat{uuid.uuid4().hex[:12]}"
    folder = f"/SAR-e2e-{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(folder)
    note = await nc_client.notes.create_note(
        title=f"Letter re Karen Smith {term}",
        content=(
            f"Jane Doe met Karen Smith about {term}. Karen wrote from "
            "karen@example.org. Later Smith left; Tom Brown took notes. "
            "Karen lives at 14 Mill Lane, XA9 8QT."
        ),
        category="",
    )
    try:
        yield term, folder, note
    finally:
        await nc_client.notes.delete_note(note["id"])
        await nc_client.webdav.delete_resource(folder)


def _assert_redacted(zf: zipfile.ZipFile, term: str) -> None:
    names = zf.namelist()
    assert "index.pdf" in names and "searches.pdf" in names
    (doc_name,) = [n for n in names if n.startswith("documents/")]
    assert "Karen" not in doc_name and "[PERSON_1]" in doc_name
    everything = " ".join(_pdf_text(zf.read(n)) for n in names)
    for leak in LEAKS:
        assert leak not in everything, leak
    assert "Jane Doe" in everything
    assert "[EMAIL_1]" in everything
    assert "[ADDRESS_1], [ADDRESS_2]" in everything  # street, then postcode
    # A bare "Karen" / "Smith" is the same person as "Karen Smith".
    assert "[PERSON_2]" in everything  # Tom Brown
    assert "[PERSON_3]" not in everything
    assert "[PERSON_1]'s letter" in _pdf_text(zf.read("index.pdf"))
    # The query log includes a search that found nothing.
    assert "no hits here" in _pdf_text(zf.read("searches.pdf"))


async def test_sar_case_via_mcp_tools(nc_mcp_login_flow_client, nc_client, workspace):
    term, folder, note = workspace
    mcp = nc_mcp_login_flow_client
    await _wait_indexed(mcp, term, note_id=note["id"])

    created = _tool_json(
        await mcp.call_tool(
            "sar_case_create",
            {"folder": folder, "name": "SAR-mcp", "subject": ["Jane Doe"]},
        )
    )
    case_id = created["case_id"]
    assert created["path"] == f"{folder}/SAR-mcp/sar-case.json"

    _tool_json(
        await mcp.call_tool(
            "sar_case_items",
            {
                "case_id": case_id,
                "add": [
                    {
                        "doc_type": "note",
                        "doc_id": note["id"],
                        "reason": "Karen Smith's letter mentions the subject",
                        "found_by": term,
                    }
                ],
                "queries": [{"text": term, "hits": 1}, {"text": "no hits here"}],
            },
        )
    )
    listed = _tool_json(await mcp.call_tool("sar_case_list", {}))
    assert any(c["case_id"] == case_id for c in listed["cases"])

    exporting = _tool_json(await mcp.call_tool("sar_case_export", {"case_id": case_id}))
    assert exporting["case"]["state"] == "exporting"

    async def get_case() -> dict:
        return _tool_json(await mcp.call_tool("sar_case_get", {"case_id": case_id}))

    done = await _wait_exported(get_case)
    assert done["case"]["state"] == "ready_for_audit"
    assert done["latest_export"]["state"] == "done"
    archive_path = done["case"]["exports"][0]["archive_path"]
    assert archive_path == f"{folder}/SAR-mcp/exports/SAR-mcp-v1.zip"
    _assert_redacted(await _archive(nc_client, archive_path), term)

    closed = _tool_json(
        await mcp.call_tool("sar_case_update", {"case_id": case_id, "state": "closed"})
    )
    assert closed["case"]["state"] == "closed"
    refused = await mcp.call_tool(
        "sar_case_items",
        {"case_id": case_id, "add": [{"doc_type": "note", "doc_id": 1, "reason": "r"}]},
    )
    assert refused.is_error is True
    assert "closed" in refused.content[0].text


async def test_sar_case_via_management_api(
    nc_mcp_login_flow_client, login_flow_static_client_token, nc_client, workspace
):
    """The routes Astrolabe calls, as the bearer token's user.

    ``nc_mcp_login_flow_client`` provisions the user's app password, which the
    case operations and the export job use.
    """
    term, folder, note = workspace
    await _wait_indexed(nc_mcp_login_flow_client, term, note_id=note["id"])
    headers = {"Authorization": f"Bearer {login_flow_static_client_token}"}

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as http:
        status = (await http.get("http://localhost:8004/api/v1/status")).json()
        assert status["sar_export_available"] is True

        response = await http.post(
            CASES, json={"folder": folder, "name": "SAR-api", "subject": ["Jane Doe"]}
        )
        assert response.status_code == 201, response.text
        case_id = response.json()["case_id"]
        case = f"{CASES}/{case_id}"

        duplicate = await http.post(
            CASES, json={"folder": folder, "name": "SAR-api", "subject": ["Jane Doe"]}
        )
        assert duplicate.status_code == 409

        response = await http.post(
            f"{case}/items",
            json={
                "add": [
                    {
                        "doc_type": "note",
                        "doc_id": note["id"],
                        "reason": "Karen Smith's letter mentions the subject",
                    }
                ],
                "queries": [{"text": term, "hits": 1}, {"text": "no hits here"}],
            },
        )
        assert response.status_code == 200, response.text

        listed = (await http.get(CASES)).json()["cases"]
        assert any(c["case_id"] == case_id for c in listed)

        response = await http.post(f"{case}/exports", json={})
        assert response.status_code == 202, response.text
        assert response.json()["case"]["state"] == "exporting"
        locked = await http.post(
            f"{case}/items",
            json={"add": [{"doc_type": "note", "doc_id": "1", "reason": "r"}]},
        )
        assert locked.status_code == 409

        async def get_case() -> dict:
            r = await http.get(case)
            assert r.status_code == 200, r.text
            return r.json()

        done = await _wait_exported(get_case)
        assert done["case"]["state"] == "ready_for_audit"
        archive_path = done["case"]["exports"][0]["archive_path"]
        _assert_redacted(await _archive(nc_client, archive_path), term)

        # Reopen, export again: a second version next to the first.
        reopened = await http.patch(case, json={"state": "open"})
        assert reopened.status_code == 200
        assert (await http.post(f"{case}/exports", json={})).status_code == 202
        again = await _wait_exported(get_case)
        assert [e["version"] for e in again["case"]["exports"]] == [1, 2]

        closed = await http.patch(case, json={"state": "closed"})
        assert closed.json()["case"]["state"] == "closed"
        assert (await http.patch(case, json={"state": "open"})).status_code == 409

        assert (await http.get(f"{CASES}/999999999")).status_code == 404
        missing = await http.post(
            CASES,
            json={
                "folder": f"/no-such-folder-{uuid.uuid4().hex[:8]}",
                "name": "SAR-x",
                "subject": ["Jane Doe"],
            },
        )
        assert missing.status_code == 403

    async with httpx.AsyncClient(timeout=30.0) as anonymous:
        assert (await anonymous.get(CASES)).status_code == 401


def _three_page_pdf(term: str) -> bytes:
    doc = pymupdf.open()
    for n, line in enumerate(
        (
            f"Page one {term}: Jane Doe started on the ward.",
            f"Page two {term}: Karen Smith raised a concern about Jane Doe.",
            f"Page three {term}: Tom Brown closed the case.",
        ),
        1,
    ):
        page = doc.new_page()
        page.insert_text((72, 72), line)
        page.insert_text((72, 100), f"End of page {n}.")
    return doc.tobytes()


async def test_sar_case_page_range_of_indexed_pdf(
    nc_mcp_login_flow_client, login_flow_static_client_token, nc_client
):
    """A page range exports only those pages of an indexed PDF, rebuilt from
    the real chunker's offsets and page numbers."""
    mcp = nc_mcp_login_flow_client
    term = f"quorvex{uuid.uuid4().hex[:12]}"
    folder = f"/SAR-e2e-{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(folder)
    await nc_client.webdav.write_file(
        f"{folder}/scan.pdf", _three_page_pdf(term), "application/pdf"
    )
    file_id = (await nc_client.webdav.get_file_info(f"{folder}/scan.pdf"))["id"]
    # Files are indexed only when tagged.
    tag = await nc_client.webdav.get_or_create_tag(
        name=get_settings().vector_sync_tag, user_visible=True, user_assignable=True
    )
    await nc_client.webdav.assign_tag_to_file(file_id, tag["id"])
    try:
        await _wait_indexed(mcp, term)
        case_id = _tool_json(
            await mcp.call_tool(
                "sar_case_create",
                {"folder": folder, "name": "SAR-pages", "subject": ["Jane Doe"]},
            )
        )["case_id"]

        # Case searches take the search filters: the folder finds it, another
        # folder does not, and both searches are logged with their folders.
        search = {"case_id": case_id, "query": term, "doc_types": ["file"]}
        inside = _tool_json(
            await mcp.call_tool(
                "sar_case_search", {**search, "path_prefixes": [folder]}
            )
        )
        assert [str(r["id"]) for r in inside["results"]] == [str(file_id)]
        outside = _tool_json(
            await mcp.call_tool(
                "sar_case_search", {**search, "path_prefixes": ["/elsewhere"]}
            )
        )
        assert outside["results"] == []
        headers = {"Authorization": f"Bearer {login_flow_static_client_token}"}
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as http:
            via_http = await http.post(
                f"{CASES}/{case_id}/search",
                json={
                    "query": term,
                    "algorithm": "hybrid",
                    "granularity": "document",
                    "doc_types": ["file"],
                    "path_prefixes": [folder],
                },
            )
        assert via_http.status_code == 200, via_http.text
        assert [str(r["id"]) for r in via_http.json()["results"]] == [str(file_id)]
        logged = _tool_json(await mcp.call_tool("sar_case_get", {"case_id": case_id}))
        assert [q["filters"]["path_prefixes"] for q in logged["case"]["queries"]] == [
            [folder],
            ["/elsewhere"],
            [folder],
        ]

        _tool_json(
            await mcp.call_tool(
                "sar_case_items",
                {
                    "case_id": case_id,
                    "add": [
                        {
                            "doc_type": "file",
                            "doc_id": file_id,
                            "reason": "concern raised",
                            "page_start": 2,
                            "page_end": 2,
                        }
                    ],
                },
            )
        )
        _tool_json(await mcp.call_tool("sar_case_export", {"case_id": case_id}))

        async def get_case() -> dict:
            return _tool_json(await mcp.call_tool("sar_case_get", {"case_id": case_id}))

        done = await _wait_exported(get_case)
        assert done["case"]["state"] == "ready_for_audit", done
        assert done["latest_export"]["failed"] == 0

        zf = await _archive(nc_client, done["case"]["exports"][0]["archive_path"])
        (doc_name,) = [n for n in zf.namelist() if n.startswith("documents/")]
        text = _pdf_text(zf.read(doc_name))
        assert "Page two" in text and "Jane Doe" in text
        assert "[PERSON_1] raised a concern" in text
        # Only the requested page: neither neighbour made it into the export.
        assert "Page one" not in text and "Page three" not in text
        assert "2-2" in _pdf_text(zf.read("index.pdf"))
    finally:
        await nc_client.webdav.delete_resource(folder)
