"""Every semantic-search result carries a usable Astrolabe chunk deep-link.

The unit tests pin the URL builder in isolation. What they cannot show is that
the tool actually reaches it with real values — the link is assembled from
fields (chunk offsets, metadata) that only a live index populates, and a result
whose offsets are missing legitimately yields no link at all. So this exercises
the whole path: index a note, search for it through MCP, and assert the returned
row carries a link Astrolabe would open.
"""

import json
import uuid
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import anyio
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from nextcloud_mcp_server.config import get_settings
from tests.integration._search_helpers import document_is_searchable

pytestmark = pytest.mark.integration

# Astrolabe's App.vue opens nothing unless all four are present.
REQUIRED_PARAMS = {"doc_type", "doc_id", "chunk_start", "chunk_end"}

INDEX_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 5

# Files index more slowly than notes (tag-gated: the scanner has to see the tag
# assignment on its next pass), and this budget must stay clear of the 180s
# pytest-timeout — at 180 the poll loop is killed before it can reach its own
# skip, turning "not indexed yet" into a hard failure.
FILE_INDEX_TIMEOUT_SECONDS = 120


async def test_search_results_carry_an_astrolabe_chunk_link(nc_mcp_client, nc_client):
    """A freshly indexed note comes back with a link that opens its chunk."""
    status = await nc_mcp_client.call_tool("nc_get_vector_sync_status", {})
    if status.is_error:
        pytest.skip("Vector sync not enabled")

    # A nonsense term the seed corpus cannot contain, so the match is ours.
    unique_term = f"zorblat{uuid.uuid4().hex[:12]}"
    note = await nc_client.notes.create_note(
        title=f"Chunk link test {unique_term}",
        content=f"This note exists to be found by the term {unique_term}.",
        category="ChunkLinkTest",
    )

    try:
        with anyio.move_on_after(INDEX_TIMEOUT_SECONDS) as scope:
            while not await document_is_searchable(
                nc_mcp_client, unique_term, note_id=note["id"]
            ):
                await anyio.sleep(POLL_INTERVAL_SECONDS)
        if scope.cancelled_caught:
            pytest.skip(
                f"note {note['id']} not indexed within {INDEX_TIMEOUT_SECONDS}s"
            )

        search = await nc_mcp_client.call_tool(
            "nc_semantic_search", {"query": unique_term, "limit": 10}
        )
        assert not search.is_error, search
        results = json.loads(search.content[0].text)["results"]

        ours = [r for r in results if str(r["id"]) == str(note["id"])]
        assert ours, f"indexed note {note['id']} missing from its own search"
        result = ours[0]

        url = result["url"]
        assert url, (
            "no chunk link on the result; the mcp service resolves "
            "nextcloud_browser_url from NEXTCLOUD_PUBLIC_ISSUER_URL, so an "
            "empty url means the builder was not reached"
        )

        # A note is not a file: only the chunk viewer opens it.
        assert result["file_url"] is None

        parsed = urlparse(url)
        assert parsed.scheme in ("http", "https")
        assert parsed.path.endswith("/apps/astrolabe/")

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert REQUIRED_PARAMS <= set(params), (
            f"Astrolabe would not open this link: {sorted(params)}"
        )
        # The link must point at THIS chunk of THIS document, not merely parse.
        assert params["doc_id"] == str(note["id"])
        assert params["doc_type"] == result["doc_type"]
        assert params["chunk_start"] == str(result["chunk_start_offset"])
        assert params["chunk_end"] == str(result["chunk_end_offset"])
    finally:
        await nc_client.notes.delete_note(note_id=note["id"])


async def test_context_expansion_preserves_the_chunk_link(nc_mcp_client, nc_client):
    """include_context=True rebuilds each result from scratch, which is exactly
    how rerank_score was once silently dropped. Same corpus, both flavours."""
    status = await nc_mcp_client.call_tool("nc_get_vector_sync_status", {})
    if status.is_error:
        pytest.skip("Vector sync not enabled")

    unique_term = f"zorblat{uuid.uuid4().hex[:12]}"
    note = await nc_client.notes.create_note(
        title=f"Chunk link context test {unique_term}",
        content=f"Context expansion should not lose the link for {unique_term}.",
        category="ChunkLinkTest",
    )

    try:
        with anyio.move_on_after(INDEX_TIMEOUT_SECONDS) as scope:
            while not await document_is_searchable(
                nc_mcp_client, unique_term, note_id=note["id"]
            ):
                await anyio.sleep(POLL_INTERVAL_SECONDS)
        if scope.cancelled_caught:
            pytest.skip(
                f"note {note['id']} not indexed within {INDEX_TIMEOUT_SECONDS}s"
            )

        search = await nc_mcp_client.call_tool(
            "nc_semantic_search",
            {"query": unique_term, "limit": 10, "include_context": True},
        )
        assert not search.is_error, search
        results = json.loads(search.content[0].text)["results"]

        ours = [r for r in results if str(r["id"]) == str(note["id"])]
        assert ours, f"indexed note {note['id']} missing from its own search"
        assert ours[0]["url"], "context expansion dropped the chunk link"
    finally:
        await nc_client.notes.delete_note(note_id=note["id"])


def _pdf_containing(term: str) -> bytes:
    """A one-page born-digital PDF whose text layer contains ``term``.

    Ordinary prose around the term on purpose: a page holding one short unspaced
    token reads as a poor text layer to the tier-0 classifier and escalates off
    the fast tier, which would make this a test of parse heuristics rather than
    of the link.
    """
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.drawString(72, 750, f"Quarterly report mentioning {term}")
    pdf.drawString(
        72,
        730,
        "This document exists so an integration test can index a real file and "
        "check the links on its search results.",
    )
    pdf.drawString(72, 710, f"The term to search for is {term}.")
    pdf.save()
    return buffer.getvalue()


async def _file_is_searchable(mcp_client, term: str, file_id: int) -> bool:
    """True once ``file_id`` is retrievable as a file result.

    Local rather than ``document_is_searchable``, which requires doc_type
    "note" when matching on an id.
    """
    try:
        search = await mcp_client.call_tool(
            "nc_semantic_search",
            arguments={"query": term, "limit": 50, "doc_types": ["file"]},
        )
    except Exception:  # transient blip — keep polling
        return False
    if search.is_error:
        return False
    try:
        results = json.loads(search.content[0].text).get("results", [])
    except (IndexError, ValueError):
        return False
    return any(str(r.get("id")) == str(file_id) for r in results)


async def test_file_results_also_carry_a_link_to_the_file(nc_mcp_client, nc_client):
    """A file result offers both links: the chunk viewer AND the file itself.

    ``url`` opens the matched passage in Astrolabe; ``file_url`` opens the
    document in Nextcloud. For doc_type="file" the doc_id IS the fileid, which
    is what makes the second link free — and this is what checks that identity
    still holds on a live index, rather than only in the indexer's source.
    """
    status = await nc_mcp_client.call_tool("nc_get_vector_sync_status", {})
    if status.is_error:
        pytest.skip("Vector sync not enabled")

    unique_term = f"zorblat{uuid.uuid4().hex[:12]}"
    test_dir = f"chunk_link_file_{uuid.uuid4().hex[:8]}"
    path = f"{test_dir}/report.pdf"
    await nc_client.webdav.create_directory(test_dir)
    # A PDF, not a text file: tagged-file discovery filters on
    # mime_type_filter="application/pdf" (vector/scanner.py), so a .txt is never
    # discovered no matter how it is tagged — this test could only ever skip.
    await nc_client.webdav.write_file(
        path, _pdf_containing(unique_term), content_type="application/pdf"
    )
    file_id = int((await nc_client.webdav.get_file_info(path))["id"])
    # File indexing is tag-gated; without the tag the scanner never sees it.
    tag = await nc_client.webdav.get_or_create_tag(
        name=get_settings().vector_sync_tag,
        user_visible=True,
        user_assignable=True,
    )
    await nc_client.webdav.assign_tag_to_file(file_id, tag["id"])

    try:
        with anyio.move_on_after(FILE_INDEX_TIMEOUT_SECONDS) as scope:
            while not await _file_is_searchable(nc_mcp_client, unique_term, file_id):
                await anyio.sleep(POLL_INTERVAL_SECONDS)
        if scope.cancelled_caught:
            pytest.skip(
                f"file {file_id} not indexed within {FILE_INDEX_TIMEOUT_SECONDS}s"
            )

        search = await nc_mcp_client.call_tool(
            "nc_semantic_search",
            {"query": unique_term, "limit": 50, "doc_types": ["file"]},
        )
        assert not search.is_error, search
        results = json.loads(search.content[0].text)["results"]

        ours = [r for r in results if str(r["id"]) == str(file_id)]
        assert ours, f"indexed file {file_id} missing from its own search"
        result = ours[0]

        assert result["file_url"], "file result carries no link to the file"
        assert result["file_url"].endswith(f"/index.php/f/{file_id}")
        # Both links, not one instead of the other.
        assert result["url"], "the chunk link was lost"
    finally:
        await nc_client.webdav.delete_resource(test_dir)
