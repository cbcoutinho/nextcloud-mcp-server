"""Qdrant client wrapper."""

import logging
import os

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

logger = logging.getLogger(__name__)


# Singleton instance
_qdrant_client: AsyncQdrantClient | None = None


async def get_qdrant_client() -> AsyncQdrantClient:
    """
    Get singleton Qdrant client instance.

    Automatically creates collection on first use if it doesn't exist.

    Returns:
        Configured AsyncQdrantClient instance

    Raises:
        Exception: If Qdrant connection fails or collection creation fails
    """
    global _qdrant_client

    if _qdrant_client is None:
        url = os.getenv("QDRANT_URL", "http://qdrant:6333")
        api_key = os.getenv("QDRANT_API_KEY")

        _qdrant_client = AsyncQdrantClient(
            url=url,
            api_key=api_key,
            timeout=30,
        )

        # Ensure collection exists
        collection_name = os.getenv("QDRANT_COLLECTION", "nextcloud_content")

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
