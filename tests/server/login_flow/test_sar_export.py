"""End-to-end: SAR export archives on the login-flow server (ADR-040).

Login flow is the deployment Astrolabe Cloud runs, and it is the one that
exercises the production paths: the export job reads and writes with the
user's stored app password (not env credentials), and the management API route
Astrolabe calls, ``/api/v1/sar/exports``, exists only in authenticated modes.

The ``mcp-login-flow`` compose service points ``EMBEDDING_GATEWAY_URL`` at the
``ner-stub`` service (``tests/fixtures/ner_stub.py``), which "detects" a fixed
set of synthetic names. Each test indexes a document, exports it, waits for the
background job, and reads the archive back from Nextcloud.
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

API = "http://localhost:8004/api/v1/sar/exports"
INDEX_TIMEOUT_SECONDS = 180
EXPORT_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 3
LEAKS = ("Karen", "Smith", "Tom Brown", "karen@example.org")


def _pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "".join(page.get_text() for page in doc)


async def _wait_indexed(mcp, term: str, note_id: int | None = None) -> None:
    with anyio.move_on_after(INDEX_TIMEOUT_SECONDS) as scope:
        while not await document_is_searchable(mcp, term, note_id=note_id):
            await anyio.sleep(POLL_INTERVAL_SECONDS)
    if scope.cancelled_caught:
        pytest.fail(f"document not indexed within {INDEX_TIMEOUT_SECONDS}s")


async def _wait_done(read_status: Callable[[], Awaitable[dict]]) -> dict:
    with anyio.fail_after(EXPORT_TIMEOUT_SECONDS):
        while (data := await read_status())["state"] == "running":
            await anyio.sleep(POLL_INTERVAL_SECONDS)
    return data


async def _archive(nc_client, path: str) -> zipfile.ZipFile:
    content, _, _ = await nc_client.webdav.read_file(path)
    return zipfile.ZipFile(io.BytesIO(content))


@pytest.fixture
async def workspace(nc_client):
    """A fresh output folder and a note naming the subject and third parties."""
    term = f"zorblat{uuid.uuid4().hex[:12]}"
    folder = f"/SAR-e2e-{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(folder)
    note = await nc_client.notes.create_note(
        title=f"Letter re Karen Smith {term}",
        content=(
            f"Jane Doe met Karen Smith about {term}. Karen wrote from "
            "karen@example.org. Later Smith left; Tom Brown took notes."
        ),
        category="",
    )
    try:
        yield term, folder, note
    finally:
        await nc_client.notes.delete_note(note["id"])
        await nc_client.webdav.delete_resource(folder)


def _assert_redacted(zf: zipfile.ZipFile) -> None:
    names = zf.namelist()
    assert "index.pdf" in names
    (doc_name,) = [n for n in names if n.startswith("documents/")]
    assert "Karen" not in doc_name and "[PERSON_1]" in doc_name
    everything = " ".join(_pdf_text(zf.read(n)) for n in names)
    for leak in LEAKS:
        assert leak not in everything, leak
    assert "Jane Doe" in everything
    assert "[EMAIL_1]" in everything
    # A bare "Karen" / "Smith" is the same person as "Karen Smith".
    assert "[PERSON_2]" in everything  # Tom Brown
    assert "[PERSON_3]" not in everything
    assert "[PERSON_1]'s letter" in _pdf_text(zf.read("index.pdf"))


async def test_sar_export_via_mcp_tools(nc_mcp_login_flow_client, nc_client, workspace):
    term, folder, note = workspace
    mcp = nc_mcp_login_flow_client
    await _wait_indexed(mcp, term, note_id=note["id"])

    submitted = await mcp.call_tool(
        "sar_export_submit",
        {
            "output_folder": folder,
            "name": "SAR-e2e",
            "subject": ["Jane Doe"],
            "items": [
                {
                    "doc_type": "note",
                    "doc_id": note["id"],
                    "reason": "Karen Smith's letter mentions the subject",
                }
            ],
            "queries": [term],
        },
    )
    assert submitted.is_error is False, submitted.content

    async def read_status() -> dict:
        result = await mcp.call_tool(
            "sar_export_status", {"output_folder": folder, "name": "SAR-e2e"}
        )
        assert result.is_error is False, result.content
        return json.loads(result.content[0].text)

    data = await _wait_done(read_status)
    assert data["state"] == "done" and data["failed"] == 0, data
    _assert_redacted(await _archive(nc_client, f"{folder}/SAR-e2e.zip"))

    # Submitting again under the same name is refused, not overwritten.
    again = await mcp.call_tool(
        "sar_export_submit",
        {
            "output_folder": folder,
            "name": "SAR-e2e",
            "subject": ["Jane Doe"],
            "items": [{"doc_type": "note", "doc_id": note["id"], "reason": "r"}],
        },
    )
    assert again.is_error is True
    assert "already exists" in again.content[0].text


async def test_sar_export_via_management_api(
    nc_mcp_login_flow_client, login_flow_static_client_token, nc_client, workspace
):
    """The route Astrolabe calls, as the bearer token's user.

    ``nc_mcp_login_flow_client`` provisions the user's app password, which the
    background job needs.
    """
    term, folder, note = workspace
    await _wait_indexed(nc_mcp_login_flow_client, term, note_id=note["id"])
    headers = {"Authorization": f"Bearer {login_flow_static_client_token}"}
    body = {
        "output_folder": folder,
        "name": "SAR-api",
        "subject": ["Jane Doe"],
        "items": [
            {
                "doc_type": "note",
                "doc_id": note["id"],
                "reason": "Karen Smith's letter mentions the subject",
            }
        ],
        "queries": [term],
    }

    async with httpx.AsyncClient(timeout=30.0) as http:
        status = (await http.get("http://localhost:8004/api/v1/status")).json()
        assert status["sar_export_available"] is True

        response = await http.post(API, json=body, headers=headers)
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "running"

        async def read_status() -> dict:
            r = await http.get(
                API,
                params={"output_folder": folder, "name": "SAR-api"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            return r.json()

        data = await _wait_done(read_status)
        assert data["state"] == "done" and data["failed"] == 0, data
        _assert_redacted(await _archive(nc_client, f"{folder}/SAR-api.zip"))

        conflict = await http.post(API, json=body, headers=headers)
        assert conflict.status_code == 409, conflict.text

        missing = await http.post(
            API,
            json={**body, "output_folder": f"/no-such-folder-{uuid.uuid4().hex[:8]}"},
            headers=headers,
        )
        assert missing.status_code == 403, missing.text
        assert "must exist and be writable" in missing.json()["message"]

        unknown = await http.get(
            API, params={"output_folder": folder, "name": "nope"}, headers=headers
        )
        assert unknown.status_code == 404

        unauthenticated = await http.post(API, json=body)
        assert unauthenticated.status_code == 401


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


async def test_sar_export_page_range_of_indexed_pdf(
    nc_mcp_login_flow_client, nc_client
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
        submitted = await mcp.call_tool(
            "sar_export_submit",
            {
                "output_folder": folder,
                "name": "SAR-pages",
                "subject": ["Jane Doe"],
                "items": [
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
        assert submitted.is_error is False, submitted.content

        async def read_status() -> dict:
            result = await mcp.call_tool(
                "sar_export_status", {"output_folder": folder, "name": "SAR-pages"}
            )
            return json.loads(result.content[0].text)

        data = await _wait_done(read_status)
        assert data["state"] == "done" and data["failed"] == 0, data

        zf = await _archive(nc_client, f"{folder}/SAR-pages.zip")
        (doc_name,) = [n for n in zf.namelist() if n.startswith("documents/")]
        text = _pdf_text(zf.read(doc_name))
        assert "Page two" in text and "Jane Doe" in text
        assert "[PERSON_1] raised a concern" in text
        # Only the requested page: neither neighbour made it into the export.
        assert "Page one" not in text and "Page three" not in text
        assert "2-2" in _pdf_text(zf.read("index.pdf"))
    finally:
        await nc_client.webdav.delete_resource(folder)
