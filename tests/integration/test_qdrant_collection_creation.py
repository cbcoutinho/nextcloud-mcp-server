"""Integration tests for Qdrant collection auto-creation.

These tests validate that:
1. Collections are automatically created on first access
2. Dimension validation detects mismatches
3. Idempotent initialization (multiple calls don't fail)
4. Proper error handling and logging
"""

from unittest.mock import Mock

import pytest
from qdrant_client.models import VectorParams

from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

pytestmark = pytest.mark.integration


def get_vector_params(collection_info) -> VectorParams:
    """Get vector params from collection info, handling named vectors format."""
    vectors = collection_info.config.params.vectors
    if isinstance(vectors, dict):
        return vectors["dense"]
    return vectors


@pytest.fixture(autouse=True)
async def reset_singleton():
    """Reset the global Qdrant client singleton between tests."""
    global _qdrant_client
    import nextcloud_mcp_server.vector.qdrant_client as qdrant_module

    # Store original
    original = qdrant_module._qdrant_client

    # Reset for test
    qdrant_module._qdrant_client = None

    yield

    # Restore original
    qdrant_module._qdrant_client = original


@pytest.mark.integration
async def test_collection_auto_created_on_first_access(monkeypatch):
    """Test that collection is automatically created if it doesn't exist."""
    # Mock settings
    from nextcloud_mcp_server.config import Settings

    mock_settings = Settings(
        qdrant_location=":memory:",
        ollama_embedding_model="nomic-embed-text",
        vector_sync_enabled=False,  # Disable background sync for test
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.get_settings", lambda: mock_settings
    )

    # Mock embedding service - must have .provider attribute
    from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider

    mock_provider = SimpleEmbeddingProvider(dimension=384)
    mock_embedding_service = Mock()
    mock_embedding_service.provider = mock_provider
    mock_embedding_service.get_dimension = lambda: mock_provider.get_dimension()
    monkeypatch.setattr(
        "nextcloud_mcp_server.embedding.get_embedding_service",
        lambda: mock_embedding_service,
    )

    # Get client (should trigger collection creation)
    client = await get_qdrant_client()

    # Verify client is initialized
    assert client is not None

    # Verify collection was created
    collection_name = mock_settings.get_collection_name()
    collections = await client.get_collections()
    collection_names = [c.name for c in collections.collections]
    assert collection_name in collection_names

    # Verify collection has correct dimensions
    collection_info = await client.get_collection(collection_name)
    assert get_vector_params(collection_info).size == 384


@pytest.mark.integration
async def test_existing_collection_reused(monkeypatch):
    """Test that existing collection is reused without error."""
    # Mock settings
    from nextcloud_mcp_server.config import Settings

    mock_settings = Settings(
        qdrant_location=":memory:",
        ollama_embedding_model="nomic-embed-text",
        vector_sync_enabled=False,
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.get_settings", lambda: mock_settings
    )

    # Mock embedding service - must have .provider attribute
    from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider

    mock_provider = SimpleEmbeddingProvider(dimension=384)
    mock_embedding_service = Mock()
    mock_embedding_service.provider = mock_provider
    mock_embedding_service.get_dimension = lambda: mock_provider.get_dimension()
    monkeypatch.setattr(
        "nextcloud_mcp_server.embedding.get_embedding_service",
        lambda: mock_embedding_service,
    )

    # First call - creates collection
    _ = await get_qdrant_client()
    collection_name = mock_settings.get_collection_name()

    # Reset singleton to simulate second initialization
    import nextcloud_mcp_server.vector.qdrant_client as qdrant_module

    qdrant_module._qdrant_client = None

    # Second call - should reuse existing collection
    client2 = await get_qdrant_client()

    # Verify both clients work
    assert client2 is not None

    # Verify collection still exists and wasn't recreated
    collections = await client2.get_collections()
    collection_names = [c.name for c in collections.collections]
    assert collection_name in collection_names

    # Verify dimensions unchanged
    collection_info = await client2.get_collection(collection_name)
    assert get_vector_params(collection_info).size == 384


@pytest.mark.integration
async def test_dimension_mismatch_detected(monkeypatch, tmp_path):
    """Test that dimension mismatch raises clear error."""
    # Use persistent temp directory so collection survives client reset
    from nextcloud_mcp_server.config import Settings

    qdrant_path = str(tmp_path / "qdrant_data")
    mock_settings = Settings(
        qdrant_location=qdrant_path,
        ollama_embedding_model="nomic-embed-text",
        vector_sync_enabled=False,
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.get_settings", lambda: mock_settings
    )

    # First embedding service: 384 dimensions
    from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider

    mock_provider_1 = SimpleEmbeddingProvider(dimension=384)
    mock_embedding_service_1 = Mock()
    mock_embedding_service_1.provider = mock_provider_1
    mock_embedding_service_1.get_dimension = lambda: mock_provider_1.get_dimension()
    monkeypatch.setattr(
        "nextcloud_mcp_server.embedding.get_embedding_service",
        lambda: mock_embedding_service_1,
    )

    # First call - creates collection with 384 dimensions
    client1 = await get_qdrant_client()
    collection_name = mock_settings.get_collection_name()

    # Verify collection created
    collection_info = await client1.get_collection(collection_name)
    assert get_vector_params(collection_info).size == 384

    # Close client1 to release file lock
    await client1.close()

    # Reset singleton (but collection persists in temp directory)
    import nextcloud_mcp_server.vector.qdrant_client as qdrant_module

    qdrant_module._qdrant_client = None

    # Change embedding service to different dimension (768)
    mock_provider_2 = SimpleEmbeddingProvider(dimension=768)
    mock_embedding_service_2 = Mock()
    mock_embedding_service_2.provider = mock_provider_2
    mock_embedding_service_2.get_dimension = lambda: mock_provider_2.get_dimension()
    monkeypatch.setattr(
        "nextcloud_mcp_server.embedding.get_embedding_service",
        lambda: mock_embedding_service_2,
    )

    # Second call - should detect dimension mismatch and raise error
    with pytest.raises(ValueError) as exc_info:
        await get_qdrant_client()

    # Verify error message is helpful
    error_msg = str(exc_info.value)
    assert "Dimension mismatch" in error_msg
    assert "384" in error_msg  # Old dimension
    assert "768" in error_msg  # New dimension
    assert "Solutions:" in error_msg  # Includes helpful solutions


@pytest.mark.integration
async def test_idempotent_initialization(monkeypatch):
    """Test that multiple calls to get_qdrant_client() are idempotent."""
    # Mock settings
    from nextcloud_mcp_server.config import Settings

    mock_settings = Settings(
        qdrant_location=":memory:",
        ollama_embedding_model="nomic-embed-text",
        vector_sync_enabled=False,
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.get_settings", lambda: mock_settings
    )

    # Mock embedding service - must have .provider attribute
    from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider

    mock_provider = SimpleEmbeddingProvider(dimension=384)
    mock_embedding_service = Mock()
    mock_embedding_service.provider = mock_provider
    mock_embedding_service.get_dimension = lambda: mock_provider.get_dimension()
    monkeypatch.setattr(
        "nextcloud_mcp_server.embedding.get_embedding_service",
        lambda: mock_embedding_service,
    )

    # Call multiple times
    client1 = await get_qdrant_client()
    client2 = await get_qdrant_client()
    client3 = await get_qdrant_client()

    # All should return same singleton instance
    assert client1 is client2
    assert client2 is client3

    # Collection should exist
    collection_name = mock_settings.get_collection_name()
    collections = await client1.get_collections()
    collection_names = [c.name for c in collections.collections]
    assert collection_name in collection_names


@pytest.mark.integration
async def test_collection_name_generation(monkeypatch):
    """Test that collection name is correctly generated from deployment ID and model."""
    # Mock settings with custom deployment ID
    from nextcloud_mcp_server.config import Settings

    mock_settings = Settings(
        qdrant_location=":memory:",
        ollama_embedding_model="test-model",
        otel_service_name="test-deployment",
        vector_sync_enabled=False,
    )

    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.get_settings", lambda: mock_settings
    )

    # Mock embedding service - must have .provider attribute
    from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider

    mock_provider = SimpleEmbeddingProvider(dimension=384)
    mock_embedding_service = Mock()
    mock_embedding_service.provider = mock_provider
    mock_embedding_service.get_dimension = lambda: mock_provider.get_dimension()
    monkeypatch.setattr(
        "nextcloud_mcp_server.embedding.get_embedding_service",
        lambda: mock_embedding_service,
    )

    # Get client
    client = await get_qdrant_client()

    # Verify collection name includes deployment ID and model
    collection_name = mock_settings.get_collection_name()
    assert "test-deployment" in collection_name or "test-model" in collection_name

    # Verify collection was created with that name
    collections = await client.get_collections()
    collection_names = [c.name for c in collections.collections]
    assert collection_name in collection_names


@pytest.mark.integration
async def test_collection_uses_cosine_distance(monkeypatch):
    """Test that created collection uses COSINE distance metric."""
    # Mock settings
    from nextcloud_mcp_server.config import Settings

    mock_settings = Settings(
        qdrant_location=":memory:",
        ollama_embedding_model="nomic-embed-text",
        vector_sync_enabled=False,
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.qdrant_client.get_settings", lambda: mock_settings
    )

    # Mock embedding service - must have .provider attribute
    from nextcloud_mcp_server.embedding import SimpleEmbeddingProvider

    mock_provider = SimpleEmbeddingProvider(dimension=384)
    mock_embedding_service = Mock()
    mock_embedding_service.provider = mock_provider
    mock_embedding_service.get_dimension = lambda: mock_provider.get_dimension()
    monkeypatch.setattr(
        "nextcloud_mcp_server.embedding.get_embedding_service",
        lambda: mock_embedding_service,
    )

    # Get client (creates collection)
    client = await get_qdrant_client()

    # Verify collection uses COSINE distance
    collection_name = mock_settings.get_collection_name()
    collection_info = await client.get_collection(collection_name)

    from qdrant_client.models import Distance

    assert get_vector_params(collection_info).distance == Distance.COSINE
