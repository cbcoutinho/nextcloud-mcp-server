"""Qdrant client wrapper."""

import logging

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.models import Distance, VectorParams

from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger(__name__)


# Singleton instance
_qdrant_client: AsyncQdrantClient | None = None


async def get_qdrant_client() -> AsyncQdrantClient:
    """
    Get singleton Qdrant client instance.

    Automatically creates collection on first use if it doesn't exist.

    Supports three Qdrant modes:
    - Network mode: QDRANT_URL set (e.g., http://qdrant:6333)
    - In-memory mode: QDRANT_LOCATION=:memory: (default if nothing configured)
    - Persistent local mode: QDRANT_LOCATION=/path/to/data

    Returns:
        Configured AsyncQdrantClient instance

    Raises:
        Exception: If Qdrant connection fails or collection creation fails
    """
    global _qdrant_client

    if _qdrant_client is None:
        settings = get_settings()

        # Detect mode and initialize client accordingly
        if settings.qdrant_url:
            # Network mode
            logger.info(f"Using Qdrant network mode: {settings.qdrant_url}")
            _qdrant_client = AsyncQdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
                timeout=30,
            )
        elif settings.qdrant_location:
            # Local mode (either :memory: or persistent path)
            if settings.qdrant_location == ":memory:":
                logger.info("Using Qdrant in-memory mode: :memory:")
                _qdrant_client = AsyncQdrantClient(":memory:")
            else:
                # Persistent local mode - use path parameter
                logger.info(f"Using Qdrant persistent mode: {settings.qdrant_location}")
                _qdrant_client = AsyncQdrantClient(path=settings.qdrant_location)
        else:
            # Should not happen due to __post_init__ validation, but handle gracefully
            logger.warning("No Qdrant mode configured, defaulting to :memory:")
            _qdrant_client = AsyncQdrantClient(":memory:")

        # Get collection name (auto-generated from deployment ID + model)
        collection_name = settings.get_collection_name()

        # Import here to avoid circular dependency
        from nextcloud_mcp_server.embedding import get_embedding_service

        embedding_service = get_embedding_service()

        # Detect dimension dynamically (for OllamaEmbeddingProvider)
        if hasattr(embedding_service.provider, "_detect_dimension"):
            await embedding_service.provider._detect_dimension()  # type: ignore[call-non-callable]

        expected_dimension = embedding_service.get_dimension()

        # Explicitly check if collection exists
        logger.debug(f"Checking if collection '{collection_name}' exists...")
        collections = await _qdrant_client.get_collections()
        collection_names = [c.name for c in collections.collections]

        if collection_name in collection_names:
            # Collection exists - validate dimensions
            logger.debug(
                f"Collection '{collection_name}' found, validating dimensions..."
            )
            collection_info = await _qdrant_client.get_collection(collection_name)
            # Handle both named vectors (dict) and legacy single vector
            vectors = collection_info.config.params.vectors
            if isinstance(vectors, dict):
                actual_dimension = vectors["dense"].size
            else:
                actual_dimension = vectors.size

            # Validate dimension matches
            if actual_dimension != expected_dimension:
                embedding_model = settings.get_embedding_model_name()
                raise ValueError(
                    f"Dimension mismatch for collection '{collection_name}':\n"
                    f"  Expected: {expected_dimension} (from embedding model '{embedding_model}')\n"
                    f"  Found: {actual_dimension}\n"
                    f"This usually means you changed the embedding model.\n"
                    f"Solutions:\n"
                    f"  1. Delete the old collection: Collection will be recreated with new dimensions\n"
                    f"  2. Set QDRANT_COLLECTION to use a different collection name\n"
                    f"  3. Revert to the original embedding model"
                )

            logger.info(
                f"Using existing Qdrant collection: {collection_name} "
                f"(dimension={actual_dimension}, model={settings.get_embedding_model_name()})"
            )

        else:
            # Collection doesn't exist - create it
            embedding_model = settings.get_embedding_model_name()
            logger.info(
                f"Collection '{collection_name}' not found, creating with "
                f"dimension={expected_dimension}, model={embedding_model}..."
            )
            await _qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config={
                    "dense": VectorParams(
                        size=expected_dimension,
                        distance=Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    "sparse": models.SparseVectorParams(
                        index=models.SparseIndexParams(
                            on_disk=False,
                        )
                    ),
                },
            )
            logger.info(
                f"Created Qdrant collection: {collection_name}\n"
                f"  Dense vector dimension: {expected_dimension}\n"
                f"  Dense embedding model: {embedding_model}\n"
                f"  Sparse vectors: BM25 (for hybrid search)\n"
                f"  Distance: COSINE\n"
                f"Background sync will index all documents with dense + sparse vectors."
            )

    return _qdrant_client
