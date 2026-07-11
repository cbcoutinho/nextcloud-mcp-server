"""Integration test for greedy page-packing (Deck #636).

Drives the real extraction path (pypdfium2 -> page_boundaries), the real
PageAwareChunker (packed vs unpacked), then indexes packed chunks into an
in-memory Qdrant and asserts the page-range citation payload survives.
"""

import pymupdf
import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nextcloud_mcp_server.document_processors.pypdfium2_fast import _extract
from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider
from nextcloud_mcp_server.vector.document_chunker import PageAwareChunker

pytestmark = pytest.mark.integration


def create_lean_multipage_pdf(n_pages: int = 12) -> bytes:
    """Create a born-digital PDF with many short (lean) pages.

    Mimics the blackbox-demo density regime: one near-empty page each, so the
    per-page chunker floor would mint one dense vector per page (Deck #636).
    """
    doc = pymupdf.open()
    for i in range(1, n_pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 50), f"Form field page {i}: value {i * 7} recorded.")
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


@pytest.fixture
async def simple_embedding_provider() -> SimpleEmbeddingProvider:
    """Simple in-process embedding provider for testing."""
    return SimpleEmbeddingProvider(dimension=384)


@pytest.fixture
async def qdrant_test_client():
    """In-memory Qdrant client for testing."""
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
async def test_collection(qdrant_test_client: AsyncQdrantClient):
    """Create + tear down a test collection in Qdrant."""
    collection_name = "test_pdf_page_packing"
    await qdrant_test_client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    )
    yield collection_name
    try:
        await qdrant_test_client.delete_collection(collection_name)
    except Exception:
        pass


async def test_page_packing_reduces_chunk_count_and_preserves_citation_e2e(
    qdrant_test_client: AsyncQdrantClient,
    test_collection: str,
    simple_embedding_provider: SimpleEmbeddingProvider,
):
    """Greedy page-packing cuts density and keeps page-range citation end-to-end."""
    pdf_bytes = create_lean_multipage_pdf(n_pages=12)
    text, metadata = _extract(pdf_bytes)
    assert metadata["page_count"] == 12
    boundaries = metadata.get("page_boundaries") or []

    unpacked = await PageAwareChunker(chunk_size=2048, pack_pages=False).chunk_text(
        text, boundaries
    )
    packed = await PageAwareChunker(chunk_size=2048, pack_pages=True).chunk_text(
        text, boundaries
    )

    # Density win: lean pages collapse from ~one-vector-per-page to far fewer.
    assert len(unpacked) == 12
    assert len(packed) < len(unpacked)

    spans_a_range = False
    for chunk in packed:
        page_start = chunk.page_number
        page_last = chunk.page_end
        assert page_start is not None and page_last is not None
        # No chunk exceeds the budget; page-range citation is well-formed.
        assert len(chunk.text) <= 2048
        assert page_start <= page_last
        # Offsets still extract exactly from the original document text.
        assert text[chunk.start_offset : chunk.end_offset] == chunk.text
        if page_last > page_start:
            spans_a_range = True
    # A packed chunk really does span a page range (first != last).
    assert spans_a_range

    # Index the packed chunks and confirm the page_end payload round-trips.
    points = []
    for idx, chunk in enumerate(packed):
        embedding = await simple_embedding_provider.embed(chunk.text)
        points.append(
            PointStruct(
                id=idx,
                vector=embedding,
                payload={
                    "doc_type": "file",
                    "file_path": "/Documents/lean-forms.pdf",
                    "page_number": chunk.page_number,
                    "page_end": chunk.page_end,
                    "page_count": metadata["page_count"],
                    "excerpt": chunk.text[:200],
                },
            )
        )
    await qdrant_test_client.upsert(
        collection_name=test_collection, points=points, wait=True
    )

    query_embedding = await simple_embedding_provider.embed("form field value recorded")
    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        limit=3,
        score_threshold=0.0,
    )
    assert len(response.points) > 0
    payload = response.points[0].payload
    assert payload is not None
    assert payload["page_end"] >= payload["page_number"]
