"""Qdrant client wrapper."""

import logging
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service

logger = logging.getLogger(__name__)


# Payload fields filtered by exact-match in scanner/processor/placeholder/eviction.
# Qdrant requires a payload index for any field used in a FieldCondition; without
# one, queries fail with HTTP 400 ("Index required but not found"). All three
# carry string values after producer normalization, so a KEYWORD index is the
# correct schema (see ADR notes in commit message).
_KEYWORD_PAYLOAD_FIELDS: tuple[str, ...] = ("doc_id", "user_id", "doc_type")

# Singleton instance
_qdrant_client: AsyncQdrantClient | None = None


async def _ensure_keyword_payload_indexes(
    client: AsyncQdrantClient, collection_name: str
) -> None:
    """Create KEYWORD payload indexes for fields used in exact-match filters.

    Idempotent at the Qdrant layer: re-creating an identical index returns
    200, so this can run on every startup. Schema conflicts (a pre-existing
    index with a different type) surface as a 400 — log loudly so operators
    can intervene, but keep going so the remaining fields still get indexed.
    """
    for field in _KEYWORD_PAYLOAD_FIELDS:
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )
            logger.info("Created KEYWORD payload index on '%s'", field)
        except UnexpectedResponse as e:
            body = getattr(e, "content", b"") or b""
            body_text = body.decode("utf-8", errors="replace")
            # 400 is the expected schema-conflict path (index already exists
            # with a different type). 5xx / network-shaped errors should not
            # be silently downgraded — keep the loop going so the remaining
            # fields still get attempted, but log at error so operators see it.
            if e.status_code == 400:
                logger.warning(
                    "Schema conflict on payload index '%s': %s", field, body_text
                )
            else:
                logger.error(
                    "Unexpected error creating payload index on '%s' (status %s): %s",
                    field,
                    e.status_code,
                    body_text,
                )


async def _backfill_doc_id_to_string(
    client: AsyncQdrantClient, collection_name: str
) -> None:
    """Rewrite legacy integer doc_id payloads to strings.

    Producers now uniformly write str(doc_id), but historical points may carry
    int values from before normalization. A KEYWORD index does not match int
    payloads, so any leftover int doc_ids would be silently invisible to
    filters. Scrolls all points once and converts in-place; idempotent (a
    second pass over the same collection performs zero writes).

    Within each scroll batch, points sharing the same int doc_id are batched
    into a single ``set_payload`` call to minimize Qdrant round-trips.
    """
    logger.info(
        "Scanning '%s' for legacy int doc_id payloads (this is a one-time "
        "migration on first start after upgrade)",
        collection_name,
    )

    rewritten = 0
    scanned = 0
    # Qdrant scroll returns next_offset as PointId | None — keep it untyped here
    # so the qdrant client's full union (UUID/int/str/PointId) flows through.
    next_offset = None
    batch_size = 256

    while True:
        points, next_offset = await client.scroll(
            collection_name=collection_name,
            limit=batch_size,
            offset=next_offset,
            with_payload=["doc_id"],
            with_vectors=False,
        )
        if not points:
            break

        # Group by stringified value so points sharing a doc_id (one document
        # → many chunks) collapse into a single set_payload call. Point IDs
        # can be int/str/UUID, so widen the value type to satisfy the qdrant
        # client's PointsSelector signature without re-spelling the union.
        by_value: dict[str, list[Any]] = {}
        for point in points:
            scanned += 1
            # Qdrant client typing allows None payload even when with_payload
            # was requested; defensive default so the type checker is happy.
            payload = point.payload or {}
            value = payload.get("doc_id")
            if value is None or isinstance(value, str):
                continue
            by_value.setdefault(str(value), []).append(point.id)

        for str_val, point_ids in by_value.items():
            # wait=True is required: _ensure_keyword_payload_indexes runs
            # immediately after this function and only indexes committed
            # data — fire-and-forget writes would leave int payloads
            # invisible to KEYWORD filters.
            await client.set_payload(
                collection_name=collection_name,
                payload={"doc_id": str_val},
                points=point_ids,
                wait=True,
            )
            rewritten += len(point_ids)

        if next_offset is None:
            break

    logger.info(
        "doc_id backfill complete: rewrote %d/%d payloads from int to str",
        rewritten,
        scanned,
    )


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
                # Type narrowing: vectors must be VectorParams if not dict
                assert isinstance(vectors, VectorParams)
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

            # Existing collections may pre-date the doc_id normalization /
            # payload-index work. Backfill before creating the index so the
            # index covers every point.
            await _backfill_doc_id_to_string(_qdrant_client, collection_name)
            await _ensure_keyword_payload_indexes(_qdrant_client, collection_name)

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
            await _ensure_keyword_payload_indexes(_qdrant_client, collection_name)

    return _qdrant_client
