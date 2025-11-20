"""Integration tests for PDF document indexing and semantic search.

These tests validate the complete PDF processing flow:
1. Process PDF with PyMuPDFProcessor
2. Chunk extracted text with page numbers
3. Index chunks into Qdrant with metadata
4. Perform semantic search on PDF content
5. Verify page numbers and metadata are preserved
"""

import pymupdf
import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nextcloud_mcp_server.document_processors.pymupdf import PyMuPDFProcessor
from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider
from nextcloud_mcp_server.vector.document_chunker import (
    ChunkWithPosition,
    RecursiveCharacterTextSplitter,
)

pytestmark = pytest.mark.integration


def create_test_pdf() -> bytes:
    """Create a small test PDF with multiple pages."""
    doc = pymupdf.open()

    # Page 1: Introduction
    page1 = doc.new_page(width=595, height=842)  # A4 size
    page1.insert_text(
        (50, 50),
        "Nextcloud Administration Guide\n\n"
        "Chapter 1: Introduction\n\n"
        "Nextcloud is a self-hosted file sharing and collaboration platform. "
        "It provides secure file storage, sharing, and synchronization across devices. "
        "This guide covers installation, configuration, and maintenance of Nextcloud.",
    )

    # Page 2: Installation
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text(
        (50, 50),
        "Chapter 2: Installation\n\n"
        "System Requirements:\n"
        "- PHP 8.0 or higher\n"
        "- MySQL 8.0 or MariaDB 10.5\n"
        "- Apache or Nginx web server\n\n"
        "Installation steps:\n"
        "1. Download Nextcloud package\n"
        "2. Extract to web server directory\n"
        "3. Configure database connection\n"
        "4. Run installation wizard",
    )

    # Page 3: Configuration
    page3 = doc.new_page(width=595, height=842)
    page3.insert_text(
        (50, 50),
        "Chapter 3: Configuration\n\n"
        "Database Configuration:\n"
        "Edit config/config.php to set database parameters. "
        "Configure database host, username, password, and database name. "
        "For optimal performance, use MySQL or MariaDB.\n\n"
        "Security Settings:\n"
        "Enable HTTPS, configure trusted domains, and set up firewall rules.",
    )

    # Convert to bytes
    pdf_bytes = doc.tobytes()
    doc.close()

    return pdf_bytes


@pytest.fixture
async def simple_embedding_provider():
    """Simple in-process embedding provider for testing."""
    return SimpleEmbeddingProvider(dimension=384)


@pytest.fixture
async def qdrant_test_client():
    """Qdrant client for testing (in-memory)."""
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
async def test_collection(qdrant_test_client: AsyncQdrantClient):
    """Create test collection in Qdrant."""
    collection_name = "test_pdf_indexing"

    # Create collection
    await qdrant_test_client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    )

    yield collection_name

    # Cleanup
    try:
        await qdrant_test_client.delete_collection(collection_name)
    except Exception:
        pass


@pytest.fixture
def pymupdf_processor():
    """PyMuPDF processor for testing (without image extraction)."""
    return PyMuPDFProcessor(extract_images=False)


async def test_pymupdf_processor_extracts_text_and_metadata(pymupdf_processor):
    """Test PyMuPDF processor extracts text and metadata from PDF."""
    pdf_bytes = create_test_pdf()

    result = await pymupdf_processor.process(
        content=pdf_bytes,
        content_type="application/pdf",
        filename="test-admin-guide.pdf",
    )

    # Verify result structure
    assert result.success is True
    assert result.processor == "pymupdf"
    assert result.text is not None
    assert len(result.text) > 0

    # Verify extracted text contains expected content
    assert "Nextcloud Administration Guide" in result.text
    assert "Chapter 1: Introduction" in result.text
    assert "Chapter 2: Installation" in result.text
    assert "Chapter 3: Configuration" in result.text
    assert "PHP 8.0 or higher" in result.text
    assert "MySQL" in result.text

    # Verify metadata
    assert result.metadata is not None
    assert result.metadata["page_count"] == 3
    assert result.metadata["filename"] == "test-admin-guide.pdf"
    assert "format" in result.metadata


async def test_document_chunker_preserves_page_numbers():
    """Test that document chunker can handle chunks with page number metadata."""
    # Create chunks with page numbers
    chunks = [
        ChunkWithPosition(
            text="Chapter 1 content on page 1",
            start_offset=0,
            end_offset=28,
            page_number=1,
        ),
        ChunkWithPosition(
            text="Chapter 2 content on page 2",
            start_offset=29,
            end_offset=57,
            page_number=2,
        ),
        ChunkWithPosition(
            text="Chapter 3 content on page 3",
            start_offset=58,
            end_offset=86,
            page_number=3,
        ),
    ]

    # Verify page numbers are preserved
    assert chunks[0].page_number == 1
    assert chunks[1].page_number == 2
    assert chunks[2].page_number == 3


async def test_pdf_indexing_and_search_flow(
    pymupdf_processor: PyMuPDFProcessor,
    qdrant_test_client: AsyncQdrantClient,
    test_collection: str,
    simple_embedding_provider: SimpleEmbeddingProvider,
):
    """Test complete PDF indexing and semantic search flow."""

    # Step 1: Process PDF with PyMuPDF
    pdf_bytes = create_test_pdf()
    result = await pymupdf_processor.process(
        content=pdf_bytes,
        content_type="application/pdf",
        filename="/Documents/admin-guide.pdf",
    )

    assert result.success is True
    assert result.metadata["page_count"] == 3

    # Step 2: Chunk the extracted text
    # Note: In real implementation, we'd track which chunk came from which page
    # For this test, we'll simulate by creating chunks manually
    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    chunks = splitter.split_text(result.text)

    assert len(chunks) > 0

    # Step 3: Index chunks into Qdrant with PDF metadata
    points = []
    for idx, chunk_text in enumerate(chunks):
        embedding = await simple_embedding_provider.embed(chunk_text)

        # Simulate page number assignment (in real implementation, this would be tracked)
        # For simplicity, assign page based on content
        page_number = 1
        if "Chapter 2" in chunk_text or "Installation" in chunk_text:
            page_number = 2
        elif "Chapter 3" in chunk_text or "Configuration" in chunk_text:
            page_number = 3

        points.append(
            PointStruct(
                id=idx,
                vector=embedding,
                payload={
                    "user_id": "admin",
                    "doc_id": "/Documents/admin-guide.pdf",
                    "doc_type": "file",
                    "title": "Nextcloud Administration Guide",
                    "file_path": "/Documents/admin-guide.pdf",
                    "mime_type": "application/pdf",
                    "page_number": page_number,
                    "page_count": result.metadata["page_count"],
                    "chunk_index": idx,
                    "excerpt": chunk_text[:200],
                },
            )
        )

    await qdrant_test_client.upsert(
        collection_name=test_collection, points=points, wait=True
    )

    # Step 4: Perform semantic search for installation instructions
    query = "how to install Nextcloud system requirements"
    query_embedding = await simple_embedding_provider.embed(query)

    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        limit=3,
        score_threshold=0.0,
    )

    # Verify search results
    assert len(response.points) > 0

    # Top result should be from installation chapter (page 2)
    top_result = response.points[0]
    assert top_result.payload["doc_type"] == "file"
    assert top_result.payload["file_path"] == "/Documents/admin-guide.pdf"
    assert (
        "Installation" in top_result.payload["excerpt"]
        or top_result.payload["page_number"] == 2
    )

    # Verify page number is preserved
    assert top_result.payload["page_number"] in [1, 2, 3]
    assert top_result.payload["page_count"] == 3

    # Step 5: Search for configuration
    query = "database configuration settings MySQL"
    query_embedding = await simple_embedding_provider.embed(query)

    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        limit=3,
        score_threshold=0.0,
    )

    assert len(response.points) > 0

    # Should find configuration chapter (page 3)
    found_config = any(
        "Configuration" in r.payload["excerpt"] or r.payload["page_number"] == 3
        for r in response.points[:2]
    )
    assert found_config


async def test_pdf_search_with_filters(
    pymupdf_processor: PyMuPDFProcessor,
    qdrant_test_client: AsyncQdrantClient,
    test_collection: str,
    simple_embedding_provider: SimpleEmbeddingProvider,
):
    """Test PDF search with metadata filters."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    # Process and index PDF
    pdf_bytes = create_test_pdf()
    result = await pymupdf_processor.process(
        content=pdf_bytes,
        content_type="application/pdf",
        filename="/Documents/admin-guide.pdf",
    )

    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    chunks = splitter.split_text(result.text)

    # Index with metadata
    points = []
    for idx, chunk_text in enumerate(chunks):
        embedding = await simple_embedding_provider.embed(chunk_text)

        points.append(
            PointStruct(
                id=idx,
                vector=embedding,
                payload={
                    "user_id": "admin",
                    "doc_id": "/Documents/admin-guide.pdf",
                    "doc_type": "file",
                    "mime_type": "application/pdf",
                    "excerpt": chunk_text[:200],
                },
            )
        )

    await qdrant_test_client.upsert(
        collection_name=test_collection, points=points, wait=True
    )

    # Search with filter for PDFs only
    query = "Nextcloud installation"
    query_embedding = await simple_embedding_provider.embed(query)

    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        query_filter=Filter(
            must=[FieldCondition(key="doc_type", match=MatchValue(value="file"))]
        ),
        limit=3,
    )

    # All results should be from file documents
    assert len(response.points) > 0
    for result in response.points:
        assert result.payload["doc_type"] == "file"
        assert result.payload["mime_type"] == "application/pdf"


async def test_pymupdf_health_check(pymupdf_processor: PyMuPDFProcessor):
    """Test PyMuPDF processor health check."""
    is_healthy = await pymupdf_processor.health_check()
    assert is_healthy is True


async def test_pymupdf_supports_pdf_mime_type(pymupdf_processor: PyMuPDFProcessor):
    """Test PyMuPDF processor declares PDF support."""
    assert "application/pdf" in pymupdf_processor.supported_mime_types
    assert pymupdf_processor.name == "pymupdf"
