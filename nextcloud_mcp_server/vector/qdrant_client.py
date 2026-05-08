"""Qdrant client wrapper."""

import logging
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service

logger = logging.getLogger(__name__)


# Payload fields filtered by exact-match in scanner/processor/placeholder/eviction.
# Qdrant requires a payload index for any field used in a FieldCondition; without
# one, queries fail with HTTP 400 ("Index required but not found"). All three
# carry string values after producer normalization, so a KEYWORD index is the
# correct schema (see ADR notes in commit message).
_KEYWORD_PAYLOAD_FIELDS: tuple[str, ...] = ("doc_id", "user_id", "doc_type")

# Sentinel point that records "this collection has been backfilled to str
# doc_id". Written after a successful pass of _backfill_doc_id_to_string so
# subsequent restarts can short-circuit the O(N) scroll. Carries no
# user_id/doc_id/doc_type, so production search filters (which always
# require user_id) never see it. In :memory: mode the sentinel does not
# survive a restart — the scroll runs every start, but is a no-op against
# an empty in-memory collection.
_DOC_ID_BACKFILL_SENTINEL_ID: str = "00000000-0000-0000-0000-d0c1d0d1d0c1"
_DOC_ID_BACKFILL_SENTINEL_PAYLOAD: dict[str, str] = {"_migration_marker": "doc_id_v1"}

# Singleton instance
_qdrant_client: AsyncQdrantClient | None = None


async def _ensure_keyword_payload_indexes(
    client: AsyncQdrantClient, collection_name: str
) -> None:
    """Create KEYWORD payload indexes for fields used in exact-match filters.

    Pre-fetches the existing payload schema and skips fields that are
    already indexed, so routine restarts make no Qdrant write round-trips
    and emit no INFO log lines. Schema conflicts (a pre-existing index
    with a different type) still surface as a 400 — log loudly so
    operators can intervene, but keep going so the remaining fields still
    get indexed.
    """
    collection_info = await client.get_collection(collection_name)
    existing_schema = collection_info.payload_schema or {}
    failed_fields: list[str] = []

    for field in _KEYWORD_PAYLOAD_FIELDS:
        if field in existing_schema:
            # Index already present — silent skip. Logging here on every
            # restart would be noise that hides the genuinely interesting
            # "first-time creation" line below.
            continue
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
                failed_fields.append(field)

    # A single per-field ERROR line is easy to miss in startup noise. Surface
    # the partial-failure summary at WARNING so operators auditing the log
    # for the post-startup state see a single line listing every missing
    # index. See docs/configuration.md for the recovery procedure.
    if failed_fields:
        logger.warning(
            "Payload index creation incomplete on '%s' — fields without indexes: %s. "
            "Searches filtering on these fields will fail with HTTP 400 "
            "(`Index required but not found`) until the next successful restart.",
            collection_name,
            ", ".join(failed_fields),
        )


async def _backfill_doc_id_to_string(
    client: AsyncQdrantClient, collection_name: str, dimension: int
) -> None:
    """Rewrite legacy integer doc_id payloads to strings.

    Producers now uniformly write str(doc_id), but historical points may carry
    int values from before normalization. A KEYWORD index does not match int
    payloads, so any leftover int doc_ids would be silently invisible to
    filters. Scrolls all points once, converts in-place, and writes a
    sentinel point on success; subsequent restarts retrieve the sentinel
    and skip the scroll entirely. Idempotent in both directions (a second
    pass on a migrated collection short-circuits via the sentinel; a
    second pass with the sentinel manually deleted is the same zero-write
    scroll the first pass would do on an already-clean collection).

    Within each scroll batch, points sharing the same int doc_id are batched
    into a single ``set_payload`` call to minimize Qdrant round-trips.

    Args:
        client: Qdrant client instance.
        collection_name: Target collection.
        dimension: Dense-vector dimension for the sentinel point's vector
            (forwarded by ``get_qdrant_client`` from the embedding model).
    """
    # Sentinel guard: if the migration ran successfully against this
    # collection on a previous start, retrieve() returns the marker point
    # and we skip the scroll. Cheap single-key lookup vs. an O(N) scroll.
    sentinel = await client.retrieve(
        collection_name=collection_name,
        ids=[_DOC_ID_BACKFILL_SENTINEL_ID],
        with_payload=False,
        with_vectors=False,
    )
    if sentinel:
        logger.debug(
            "doc_id backfill sentinel found on '%s'; skipping scroll",
            collection_name,
        )
        return

    logger.info(
        "Running doc_id backfill on '%s' (one-time migration on first "
        "start after upgrade; subsequent restarts skip via sentinel)",
        collection_name,
    )

    rewritten = 0
    scanned = 0
    batch_num = 0
    # Qdrant scroll returns next_offset as PointId | None — keep it untyped here
    # so the qdrant client's full union (UUID/int/str/PointId) flows through.
    next_offset = None
    batch_size = 256
    # Log progress every N batches so a long-running migration on a large
    # collection (≥ 50k points) doesn't look like a startup hang. At batch
    # size 256, every 20 batches ≈ 5 120 points scanned.
    progress_log_every = 20

    # A transient Qdrant failure mid-scroll (network blip, timeout) must not
    # crash startup. The singleton in get_qdrant_client is already assigned
    # by the time this runs, so re-raising here would leave the process in
    # a half-initialized state where the next call returns the cached
    # client and skips this migration entirely. Catch broadly, log with
    # exc_info, and return without writing the sentinel — the next process
    # restart will retry from scratch. The sentinel write is NOT covered by
    # this try/except: a failure there means the data migration succeeded
    # and only the short-circuit marker is missing, which is a different
    # (and milder) condition than a scroll failure.
    try:
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

            batch_num += 1

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
                # wait=True is required because _ensure_keyword_payload_indexes
                # runs immediately after this function (see get_qdrant_client
                # near the call site) and only indexes committed data —
                # fire-and-forget writes would leave int payloads invisible
                # to KEYWORD filters.
                await client.set_payload(
                    collection_name=collection_name,
                    payload={"doc_id": str_val},
                    points=point_ids,
                    wait=True,
                )
                rewritten += len(point_ids)

            if batch_num % progress_log_every == 0:
                logger.info(
                    "doc_id backfill progress on '%s': scanned %d points, "
                    "rewrote %d so far",
                    collection_name,
                    scanned,
                    rewritten,
                )

            if next_offset is None:
                break
    except Exception:
        logger.error(
            "doc_id backfill scroll failed on '%s'; will retry on next restart",
            collection_name,
            exc_info=True,
        )
        return

    # Data backfill succeeded — write the sentinel so a future restart can
    # short-circuit. Empty sparse vector mirrors the placeholder.py
    # convention (vector/placeholder.py); zero dense vector is fine
    # because the sentinel never participates in a search (no user_id /
    # doc_id / doc_type payload to match). A failure here is non-fatal:
    # the data is correct; only the short-circuit marker is missing, so
    # the next restart will re-scroll an already-clean collection (idempotent
    # zero-write) before retrying the upsert.
    sentinel_point = PointStruct(
        id=_DOC_ID_BACKFILL_SENTINEL_ID,
        vector={
            "dense": [0.0] * dimension,
            "sparse": models.SparseVector(indices=[], values=[]),
        },
        payload=dict(_DOC_ID_BACKFILL_SENTINEL_PAYLOAD),
    )
    try:
        await client.upsert(
            collection_name=collection_name,
            points=[sentinel_point],
            wait=True,
        )
    except Exception:
        logger.warning(
            "doc_id backfill data succeeded on '%s' but sentinel write failed; "
            "next restart will re-scroll (idempotent zero-write on clean collection)",
            collection_name,
            exc_info=True,
        )
        return

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
            await _backfill_doc_id_to_string(
                _qdrant_client, collection_name, expected_dimension
            )
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
