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

        # Ensure collection exists
        collection_name = settings.qdrant_collection

        # Import here to avoid circular dependency
        from nextcloud_mcp_server.embedding import get_embedding_service

        embedding_service = get_embedding_service()
        dimension = embedding_service.get_dimension()

        try:
            await _qdrant_client.get_collection(collection_name)
            logger.info(f"Using existing Qdrant collection: {collection_name}")
        except Exception:
            # Collection doesn't exist, create it
            await _qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=dimension,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(
                f"Created Qdrant collection: {collection_name} "
                f"(dimension={dimension}, distance=COSINE)"
            )

    return _qdrant_client
