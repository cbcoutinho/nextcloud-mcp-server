"""Regression guard for the never-converging ingest loop (Deck #1084 / #509).

Chunk point IDs are ``uuid5("<doc_type>:<doc_id>:chunk:<i>")`` — deterministic in
the chunk INDEX but blind to the chunk COUNT. So a document re-indexed into fewer
chunks than last time overwrites ``0..N-1`` and STRANDS every higher-index point,
which keeps the previous run's ``etag`` / ``index_mode`` / ``embedding_identity``.

Both "already indexed?" reads pick a single point via an unordered
``scroll(..., limit=1)``, so either can be handed an orphan and conclude the
document needs re-indexing — forever. In production that re-OCR'd the same 44
PDFs on the burst GPU every scan cycle, indefinitely.

``prune_stale_chunks`` closes it. These tests pin the two things that matter: the
range is anchored at the NEW chunk count, and a zero-point index never nukes a
good one.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import Filter

from nextcloud_mcp_server.vector.processor import prune_stale_chunks

pytestmark = pytest.mark.unit


def _client() -> SimpleNamespace:
    return SimpleNamespace(delete=AsyncMock())


def _conditions(selector: Filter) -> dict:
    """Flatten the selector's must-conditions into {payload key: condition}."""
    return {c.key: c for c in selector.must}


async def test_prunes_chunks_above_the_new_count():
    """A 3-chunk re-index must delete chunk_index >= 3, and nothing below it."""
    client = _client()

    await prune_stale_chunks(
        client,
        collection_name="tenant-x",
        doc_id="1001",
        doc_type="file",
        kept_chunks=3,
    )

    client.delete.assert_awaited_once()
    kwargs = client.delete.await_args.kwargs
    assert kwargs["collection_name"] == "tenant-x"

    by_key = _conditions(kwargs["points_selector"])
    # Scoped to this document only — point IDs are user-agnostic, so a doc_id +
    # doc_type filter is the whole document across every reader.
    assert by_key["doc_id"].match.value == "1001"
    assert by_key["doc_type"].match.value == "file"
    # gte (not gt): chunk 3 is the first orphan when 3 chunks (0,1,2) were kept.
    assert by_key["chunk_index"].range.gte == 3
    assert by_key["chunk_index"].range.lt is None  # unbounded above


async def test_zero_chunks_never_deletes():
    """A zero-point 'success' must not wipe a previously good index.

    The point loop zips chunks with sparse_embeddings and zip truncates silently,
    so len(points) can legitimately reach 0 on a broken embedding batch. Pruning
    from 0 would delete every chunk the document has.
    """
    client = _client()

    await prune_stale_chunks(
        client,
        collection_name="tenant-x",
        doc_id="1001",
        doc_type="file",
        kept_chunks=0,
    )

    client.delete.assert_not_awaited()


async def test_delete_failure_never_fails_the_index():
    """A transient Qdrant blip must not propagate out of a successful index.

    The caller sits inside process_document's retry loop, so raising here would
    re-download/re-parse/re-embed on every attempt and, once they're exhausted,
    dead-letter a file that is sitting correctly in the index — the very
    "re-processing forever" class this function exists to close. Orphans are
    harmless until a read happens to pick one, and the next prune clears them.
    """
    client = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("qdrant down")))

    await prune_stale_chunks(
        client,
        collection_name="tenant-x",
        doc_id="1001",
        doc_type="file",
        kept_chunks=3,
    )

    client.delete.assert_awaited_once()


async def test_single_chunk_document_still_prunes():
    """The common OCR shape: a doc that used to chunk long and now yields one."""
    client = _client()

    await prune_stale_chunks(
        client,
        collection_name="tenant-x",
        doc_id="1002",
        doc_type="file",
        kept_chunks=1,
    )

    by_key = _conditions(client.delete.await_args.kwargs["points_selector"])
    assert by_key["chunk_index"].range.gte == 1
