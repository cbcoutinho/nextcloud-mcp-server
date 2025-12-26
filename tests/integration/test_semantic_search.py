"""Integration tests for semantic search with vector database.

These tests validate the complete semantic search flow:
1. Initialize Qdrant collection with simple in-process embeddings
2. Index sample notes into vector database
3. Perform semantic search queries
4. Verify relevant results are returned

Uses SimpleEmbeddingProvider for deterministic, in-process embeddings
without requiring external services like Ollama.
"""

import math
import tempfile
from pathlib import Path

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider

pytestmark = pytest.mark.integration


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
    collection_name = "test_semantic_search"

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
def sample_notes():
    """Sample notes for testing semantic search."""
    return [
        {
            "id": 1,
            "title": "Python Async Programming",
            "content": """# Python Async/Await Patterns

## Key Concepts
- Use async def for coroutines
- Use await for async operations
- asyncio.gather() for parallel execution

## Best Practices
Always use async context managers for resources.
Avoid blocking operations in async code.""",
            "category": "Development",
        },
        {
            "id": 2,
            "title": "Book Recommendations 2025",
            "content": """# Books to Read

## Fiction
- The Midnight Library by Matt Haig
- Project Hail Mary by Andy Weir

## Non-Fiction
- Atomic Habits by James Clear
- Deep Work by Cal Newport

## Technical
- Designing Data-Intensive Applications by Martin Kleppmann""",
            "category": "Personal",
        },
        {
            "id": 3,
            "title": "Chocolate Chip Cookie Recipe",
            "content": """# Classic Cookies

## Ingredients
- 2 cups flour
- 1 cup butter
- 1 cup sugar
- 2 eggs
- 2 cups chocolate chips

## Instructions
1. Preheat oven to 375°F
2. Mix butter and sugar
3. Add eggs and vanilla
4. Mix in flour
5. Fold in chocolate chips
6. Bake 10-12 minutes""",
            "category": "Recipes",
        },
        {
            "id": 4,
            "title": "Team Meeting Notes",
            "content": """# Q1 Planning Meeting

## Attendees
- Alice, Bob, Charlie

## Discussion
- Review Q4 deliverables
- Plan Q1 sprints
- Resource allocation

## Action Items
- Alice: Draft timeline
- Bob: Infrastructure review""",
            "category": "Work",
        },
    ]


async def test_simple_embedding_provider_deterministic(simple_embedding_provider):
    """Test that SimpleEmbeddingProvider generates deterministic embeddings."""
    text = "Hello world this is a test"

    # Generate embedding twice
    embedding1 = await simple_embedding_provider.embed(text)
    embedding2 = await simple_embedding_provider.embed(text)

    # Should be identical
    assert embedding1 == embedding2
    assert len(embedding1) == 384

    # Should be normalized (unit length)

    norm = math.sqrt(sum(x * x for x in embedding1))
    assert abs(norm - 1.0) < 1e-6


async def test_simple_embedding_provider_similarity(simple_embedding_provider):
    """Test that similar texts have higher cosine similarity."""

    async def cosine_similarity(text1: str, text2: str) -> float:
        emb1 = await simple_embedding_provider.embed(text1)
        emb2 = await simple_embedding_provider.embed(text2)
        return sum(a * b for a, b in zip(emb1, emb2))

    # Similar texts
    python_text1 = "Python async programming with asyncio"
    python_text2 = "Using async and await in Python"
    unrelated_text = "Chocolate chip cookie recipe"

    # Similar texts should have higher similarity
    similar_score = await cosine_similarity(python_text1, python_text2)
    unrelated_score = await cosine_similarity(python_text1, unrelated_text)

    assert similar_score > unrelated_score
    assert similar_score > 0.3  # Some semantic overlap
    assert unrelated_score < similar_score


async def test_semantic_search_with_qdrant(
    qdrant_test_client: AsyncQdrantClient,
    test_collection: str,
    simple_embedding_provider: SimpleEmbeddingProvider,
    sample_notes: list[dict],
):
    """Test full semantic search flow with Qdrant."""

    # Index all sample notes
    points = []
    for note in sample_notes:
        content = f"{note['title']}\n\n{note['content']}"
        embedding = await simple_embedding_provider.embed(content)

        points.append(
            PointStruct(
                id=note["id"],  # Use integer ID for in-memory Qdrant
                vector=embedding,
                payload={
                    "note_id": note["id"],
                    "title": note["title"],
                    "category": note["category"],
                    "excerpt": content[:200],
                },
            )
        )

    await qdrant_test_client.upsert(
        collection_name=test_collection, points=points, wait=True
    )

    # Test Query 1: Search for Python programming
    query = "async programming patterns in Python"
    query_embedding = await simple_embedding_provider.embed(query)

    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        limit=3,
        score_threshold=0.0,
    )

    # Should find Python note as top result
    assert len(response.points) > 0
    assert response.points[0].payload["note_id"] == 1
    assert "Python" in response.points[0].payload["title"]

    # Test Query 2: Search for books
    query = "good books to read recommendations"
    query_embedding = await simple_embedding_provider.embed(query)

    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        limit=3,
        score_threshold=0.0,
    )

    # Should find book recommendations note
    assert len(response.points) > 0
    top_result = response.points[0]
    assert top_result.payload["note_id"] == 2
    assert "Book" in top_result.payload["title"]

    # Test Query 3: Search for recipes
    query = "how to bake cookies dessert"
    query_embedding = await simple_embedding_provider.embed(query)

    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        limit=3,
        score_threshold=0.0,
    )

    # Should find recipe note
    assert len(response.points) > 0
    # Recipe should be in top 2 results
    top_note_ids = [r.payload["note_id"] for r in response.points[:2]]
    assert 3 in top_note_ids


async def test_semantic_search_with_filters(
    qdrant_test_client: AsyncQdrantClient,
    test_collection: str,
    simple_embedding_provider: SimpleEmbeddingProvider,
    sample_notes: list[dict],
):
    """Test semantic search with category filtering."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    # Index notes
    points = []
    for note in sample_notes:
        content = f"{note['title']}\n\n{note['content']}"
        embedding = await simple_embedding_provider.embed(content)

        points.append(
            PointStruct(
                id=note["id"],  # Use integer ID for in-memory Qdrant
                vector=embedding,
                payload={
                    "note_id": note["id"],
                    "title": note["title"],
                    "category": note["category"],
                },
            )
        )

    await qdrant_test_client.upsert(
        collection_name=test_collection, points=points, wait=True
    )

    # Search only in "Personal" category
    query = "books reading"
    query_embedding = await simple_embedding_provider.embed(query)

    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        query_filter=Filter(
            must=[FieldCondition(key="category", match=MatchValue(value="Personal"))]
        ),
        limit=3,
    )

    # Should only return Personal category notes
    assert len(response.points) > 0
    for result in response.points:
        assert result.payload["category"] == "Personal"


async def test_semantic_search_empty_results(
    qdrant_test_client: AsyncQdrantClient,
    test_collection: str,
    simple_embedding_provider: SimpleEmbeddingProvider,
):
    """Test semantic search with no indexed content returns empty results."""

    query = "test query"
    query_embedding = await simple_embedding_provider.embed(query)

    response = await qdrant_test_client.query_points(
        collection_name=test_collection,
        query=query_embedding,
        limit=10,
    )

    assert len(response.points) == 0


async def test_batch_embedding(simple_embedding_provider: SimpleEmbeddingProvider):
    """Test batch embedding generation."""
    texts = [
        "First document about Python",
        "Second document about JavaScript",
        "Third document about TypeScript",
    ]

    embeddings = await simple_embedding_provider.embed_batch(texts)

    assert len(embeddings) == 3
    assert all(len(emb) == 384 for emb in embeddings)

    # Each should be normalized

    for emb in embeddings:
        norm = math.sqrt(sum(x * x for x in emb))
        assert abs(norm - 1.0) < 1e-6


async def test_qdrant_persistent_mode(
    simple_embedding_provider: SimpleEmbeddingProvider,
    sample_notes: list[dict],
):
    """Test Qdrant in persistent local mode with file storage."""

    with tempfile.TemporaryDirectory() as tmpdir:
        storage_path = Path(tmpdir) / "qdrant_data"

        # Create first client with persistent storage using path parameter
        client1 = AsyncQdrantClient(path=str(storage_path))

        try:
            collection_name = "test_persistent"

            # Create collection and index notes
            await client1.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=384, distance=Distance.COSINE),
            )

            # Index sample notes
            points = []
            for note in sample_notes:
                content = f"{note['title']}\n\n{note['content']}"
                embedding = await simple_embedding_provider.embed(content)

                points.append(
                    PointStruct(
                        id=note["id"],
                        vector=embedding,
                        payload={
                            "note_id": note["id"],
                            "title": note["title"],
                            "category": note["category"],
                        },
                    )
                )

            await client1.upsert(
                collection_name=collection_name, points=points, wait=True
            )

            # Verify data was written
            count_result = await client1.count(collection_name=collection_name)
            assert count_result.count == len(sample_notes)

            # Close first client
            await client1.close()

            # Create new client with same storage path
            client2 = AsyncQdrantClient(path=str(storage_path))

            try:
                # Data should persist - verify collection exists
                collections = await client2.get_collections()
                collection_names = [c.name for c in collections.collections]
                assert collection_name in collection_names

                # Verify indexed data persisted
                count_result = await client2.count(collection_name=collection_name)
                assert count_result.count == len(sample_notes)

                # Verify search still works
                query = "Python programming"
                query_embedding = await simple_embedding_provider.embed(query)

                response = await client2.query_points(
                    collection_name=collection_name,
                    query=query_embedding,
                    limit=3,
                )

                # Should find Python note as top result
                assert len(response.points) > 0
                assert response.points[0].payload["note_id"] == 1

            finally:
                await client2.close()

        finally:
            # Cleanup
            await client1.close()
