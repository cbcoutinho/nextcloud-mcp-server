"""End-to-end: SAR export archive against the running stack (ADR-040).

The ``mcp`` compose service points ``EMBEDDING_GATEWAY_URL`` at the ``ner-stub``
service (``tests/fixtures/ner_stub.py``), which "detects" a fixed set of
synthetic names. The test indexes a note, submits it for export through MCP,
waits for the background job, and reads the archive back from Nextcloud.
"""

import io
import json
import uuid
import zipfile

import anyio
import pymupdf
import pytest

from tests.integration._search_helpers import document_is_searchable

pytestmark = pytest.mark.integration

INDEX_TIMEOUT_SECONDS = 150
EXPORT_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 3


def _pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "".join(page.get_text() for page in doc)


async def _status(nc_mcp_client, folder: str, name: str) -> dict:
    result = await nc_mcp_client.call_tool(
        "sar_export_status", {"output_folder": folder, "name": name}
    )
    assert result.is_error is False, result.content
    return json.loads(result.content[0].text)


async def test_sar_export_writes_redacted_archive(nc_mcp_client, nc_client):
    status = await nc_mcp_client.call_tool("nc_get_vector_sync_status", {})
    if status.is_error:
        pytest.skip("Vector sync not enabled")

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
        with anyio.move_on_after(INDEX_TIMEOUT_SECONDS) as scope:
            while not await document_is_searchable(
                nc_mcp_client, term, note_id=note["id"]
            ):
                await anyio.sleep(POLL_INTERVAL_SECONDS)
        if scope.cancelled_caught:
            pytest.skip(f"Note not indexed within {INDEX_TIMEOUT_SECONDS}s")

        submitted = await nc_mcp_client.call_tool(
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

        with anyio.fail_after(EXPORT_TIMEOUT_SECONDS):
            while (data := await _status(nc_mcp_client, folder, "SAR-e2e"))[
                "state"
            ] == "running":
                await anyio.sleep(POLL_INTERVAL_SECONDS)
        assert data["state"] == "done", data
        assert data["failed"] == 0, data

        content, _, _ = await nc_client.webdav.read_file(f"{folder}/SAR-e2e.zip")
        zf = zipfile.ZipFile(io.BytesIO(content))
        names = zf.namelist()
        assert "index.pdf" in names and "searches.pdf" in names
        (doc_name,) = [n for n in names if n.startswith("documents/")]
        assert "Karen" not in doc_name and "[PERSON_1]" in doc_name

        everything = " ".join(_pdf_text(zf.read(n)) for n in names)
        for leak in ("Karen", "Smith", "Tom Brown", "karen@example.org"):
            assert leak not in everything, leak
        assert "Jane Doe" in everything
        assert "[EMAIL_1]" in everything
        assert "[PERSON_1]'s letter" in _pdf_text(zf.read("index.pdf"))

        # Submitting again under the same name is refused, not overwritten.
        again = await nc_mcp_client.call_tool(
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
    finally:
        await nc_client.notes.delete_note(note["id"])
        await nc_client.webdav.delete_resource(folder)


async def test_sar_export_refuses_missing_folder(nc_mcp_client):
    status = await nc_mcp_client.call_tool("nc_get_vector_sync_status", {})
    if status.is_error:
        pytest.skip("Vector sync not enabled")

    result = await nc_mcp_client.call_tool(
        "sar_export_submit",
        {
            "output_folder": f"/no-such-folder-{uuid.uuid4().hex[:8]}",
            "name": "SAR-x",
            "subject": ["Jane Doe"],
            "items": [{"doc_type": "note", "doc_id": "1", "reason": "r"}],
        },
    )
    assert result.is_error is True
    assert "must exist and be writable" in result.content[0].text
