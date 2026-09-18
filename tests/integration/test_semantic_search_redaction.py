"""End-to-end: redacted ``nc_semantic_search`` against the running stack (ADR-038).

The ``mcp`` compose service points ``EMBEDDING_GATEWAY_URL`` at the ``ner-stub``
service (``tests/fixtures/ner_stub.py``), which "detects" a fixed set of
synthetic names. So indexing stores person names on the note's chunks, and the
search redacts from them. ``include_context`` also exercises the live-detection
path, because context text comes from neighbouring chunks.
"""

import json
import uuid

import anyio
import pytest

from tests.integration._search_helpers import document_is_searchable

pytestmark = pytest.mark.integration

INDEX_TIMEOUT_SECONDS = 150
POLL_INTERVAL_SECONDS = 5


async def test_semantic_search_redacts_third_parties(nc_mcp_client, nc_client):
    status = await nc_mcp_client.call_tool("nc_get_vector_sync_status", {})
    if status.is_error:
        pytest.skip("Vector sync not enabled")

    term = f"zorblat{uuid.uuid4().hex[:12]}"
    note = await nc_client.notes.create_note(
        title=f"Letter re Karen Smith {term}",
        content=(
            f"Jane Doe met Karen Smith about {term}. Later Smith left the meeting."
        ),
        # A name that appears ONLY in the category, which ingest never scans:
        # it must still be redacted (detected live).
        category="Tom Brown",
    )
    try:
        with anyio.move_on_after(INDEX_TIMEOUT_SECONDS) as scope:
            while not await document_is_searchable(
                nc_mcp_client, term, note_id=note["id"]
            ):
                await anyio.sleep(POLL_INTERVAL_SECONDS)
        if scope.cancelled_caught:
            pytest.skip(f"Note not indexed within {INDEX_TIMEOUT_SECONDS}s")

        result = await nc_mcp_client.call_tool(
            "nc_semantic_search",
            {
                "query": term,
                "doc_types": ["note"],
                "limit": 5,
                "redact": True,
                "keep_names": ["Jane Doe"],
                "include_context": True,
            },
        )
        assert result.is_error is False, result.content
        data = json.loads(result.content[0].text)
        rows = [r for r in data["results"] if r["id"] == note["id"]]
        assert rows, data

        assert data["redaction"]["applied"] is True
        for row in rows:
            returned = " ".join(
                str(row.get(field) or "")
                for field in (
                    "title",
                    "category",
                    "excerpt",
                    "url",
                    "marked_text",
                    "before_context",
                    "after_context",
                )
            )
            assert "Karen" not in returned and "Smith" not in returned, row
            assert "Tom Brown" not in returned, row
        assert "Jane Doe" in rows[0]["excerpt"]
        assert "[PERSON_" in rows[0]["excerpt"]
    finally:
        await nc_client.notes.delete_note(note["id"])
