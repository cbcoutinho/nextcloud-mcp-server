"""Qdrant client wrapper."""

import logging

from qdrant_client import AsyncQdrantClient
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
        expected_dimension = embedding_service.get_dimension()

        try:
            # Get existing collection
            collection_info = await _qdrant_client.get_collection(collection_name)
            actual_dimension = collection_info.config.params.vectors.size

            # Validate dimension matches
            if actual_dimension != expected_dimension:
                raise ValueError(
                    f"Dimension mismatch for collection '{collection_name}':\n"
                    f"  Expected: {expected_dimension} (from embedding model '{settings.ollama_embedding_model}')\n"
                    f"  Found: {actual_dimension}\n"
                    f"This usually means you changed the embedding model.\n"
                    f"Solutions:\n"
                    f"  1. Delete the old collection: Collection will be recreated with new dimensions\n"
                    f"  2. Set QDRANT_COLLECTION to use a different collection name\n"
                    f"  3. Revert OLLAMA_EMBEDDING_MODEL to the original model"
                )

            logger.info(
                f"Using existing Qdrant collection: {collection_name} "
                f"(dimension={actual_dimension}, model={settings.ollama_embedding_model})"
            )

        except Exception as e:
            # Check if it's a dimension mismatch error (re-raise it)
            if isinstance(e, ValueError):
                raise

            # Collection doesn't exist, create it
            await _qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=expected_dimension,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(
                f"Created Qdrant collection: {collection_name}\n"
                f"  Dimension: {expected_dimension}\n"
                f"  Model: {settings.ollama_embedding_model}\n"
                f"  Distance: COSINE\n"
                f"Background sync will index all documents with this embedding model."
            )

    return _qdrant_client
