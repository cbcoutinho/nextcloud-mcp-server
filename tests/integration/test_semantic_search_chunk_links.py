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
from urllib.parse import parse_qs, urlparse

import anyio
import pytest

from tests.integration._search_helpers import document_is_searchable

pytestmark = pytest.mark.integration

# Astrolabe's App.vue opens nothing unless all four are present.
REQUIRED_PARAMS = {"doc_type", "doc_id", "chunk_start", "chunk_end"}

INDEX_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 5


async def test_search_results_carry_an_astrolabe_chunk_link(nc_mcp_client, nc_client):
    """A freshly indexed note comes back with a link that opens its chunk."""
    status = await nc_mcp_client.call_tool("nc_get_vector_sync_status", {})
    if status.isError:
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
        assert not search.isError, search
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
    if status.isError:
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
        assert not search.isError, search
        results = json.loads(search.content[0].text)["results"]

        ours = [r for r in results if str(r["id"]) == str(note["id"])]
        assert ours, f"indexed note {note['id']} missing from its own search"
        assert ours[0]["url"], "context expansion dropped the chunk link"
    finally:
        await nc_client.notes.delete_note(note_id=note["id"])
