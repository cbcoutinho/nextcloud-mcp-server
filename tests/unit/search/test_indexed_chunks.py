"""indexed_chunks: every chunk of one document, read from the index (plugin API)."""

import pytest

from nextcloud_mcp_server.search import context

pytestmark = pytest.mark.unit


class FakePoint:
    def __init__(self, payload):
        self.payload = payload


class FakeQdrant:
    """Two scroll pages; chunk 1 is duplicated (indexed under two owners)."""

    def __init__(self, payloads):
        self.pages = [payloads[:2], payloads[2:]]
        self.calls = []

    async def scroll(self, *, offset, **kwargs):
        self.calls.append(kwargs)
        page = 0 if offset is None else 1
        return [FakePoint(p) for p in self.pages[page]], (1 if page == 0 else None)


async def test_pages_through_and_keeps_one_payload_per_chunk(monkeypatch):
    fake = FakeQdrant(
        [
            {"chunk_index": 0, "excerpt": "a"},
            {"chunk_index": 1, "excerpt": "b"},
            {"chunk_index": 1, "excerpt": "b"},
            {"chunk_index": 2, "excerpt": "c"},
        ]
    )

    async def get_qdrant_client():
        return fake

    monkeypatch.setattr(context, "get_qdrant_client", get_qdrant_client)

    chunks = await context.indexed_chunks(
        "dpo", ["dpo", "owner"], "5", "file", ["excerpt"]
    )

    assert sorted(c["excerpt"] for c in chunks) == ["a", "b", "c"]
    assert len(fake.calls) == 2
    # chunk_index is always fetched: it is what deduplicates.
    assert fake.calls[0]["with_payload"] == ["excerpt", "chunk_index"]
    assert fake.calls[0]["with_vectors"] is False
