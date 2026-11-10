"""``nc_semantic_search(algorithm=...)`` against the live stack.

Indexes two notes through the real sync, then searches them through MCP with
each algorithm. The unit tests stub the algorithms; this shows the tool reaches
real Qdrant with each one, and that ``exact`` is a phrase match (the note with
the words apart is excluded) with a true ``total_found``.
"""

import json
import uuid

import anyio
import pytest

from tests.integration._search_helpers import document_is_searchable

pytestmark = pytest.mark.integration

INDEX_TIMEOUT_SECONDS = 150
POLL_INTERVAL_SECONDS = 5


async def _search(nc_mcp_client, **arguments) -> dict:
    result = await nc_mcp_client.call_tool("nc_semantic_search", arguments)
    assert not result.is_error, result
    return json.loads(result.content[0].text)


async def test_every_algorithm_is_selectable_and_exact_is_a_phrase_match(
    nc_mcp_client, nc_client
):
    status = await nc_mcp_client.call_tool("nc_get_vector_sync_status", {})
    if status.is_error:
        pytest.skip("Vector sync not enabled")

    # Nonsense words the seed corpus cannot contain, so every match is ours.
    first, second = (f"zorblat{uuid.uuid4().hex[:10]}" for _ in range(2))
    phrase = f"{first} {second}"
    together = await nc_client.notes.create_note(
        title="Exact phrase test",
        content=f"This note mentions {phrase} as one phrase.",
        category="ExactSearchTest",
    )
    apart = await nc_client.notes.create_note(
        title="Exact phrase test (words apart)",
        content=f"This note mentions {second}, and later, {first}.",
        category="ExactSearchTest",
    )

    try:
        with anyio.move_on_after(INDEX_TIMEOUT_SECONDS) as scope:
            for note in (together, apart):
                while not await document_is_searchable(
                    nc_mcp_client, first, note_id=note["id"]
                ):
                    await anyio.sleep(POLL_INTERVAL_SECONDS)
        if scope.cancelled_caught:
            pytest.skip(f"notes not indexed within {INDEX_TIMEOUT_SECONDS}s")

        default = await _search(nc_mcp_client, query=phrase)
        assert default["search_method"].startswith("bm25_hybrid_")

        for algorithm in ("semantic", "bm25"):
            body = await _search(nc_mcp_client, query=phrase, algorithm=algorithm)
            assert body["search_method"] == algorithm
            assert body["truncated"] is None

        exact = await _search(
            nc_mcp_client, query=phrase, algorithm="exact", doc_types=["note"]
        )
        assert exact["search_method"] == "exact"
        assert exact["granularity"] == "document"
        assert exact["truncated"] is False
        ids = {r["id"] for r in exact["results"]}
        assert ids == {together["id"]}, ids
        assert exact["total_found"] == 1
        assert exact["results"][0]["relevance"] == 1.0

        # Paging past the end: empty page, same true total.
        past = await _search(
            nc_mcp_client,
            query=phrase,
            algorithm="exact",
            doc_types=["note"],
            offset=1,
        )
        assert past["results"] == []
        assert past["total_found"] == 1
    finally:
        for note in (together, apart):
            await nc_client.notes.delete_note(note_id=note["id"])
