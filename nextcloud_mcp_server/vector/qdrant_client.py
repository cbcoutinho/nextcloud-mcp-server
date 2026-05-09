"""Qdrant client wrapper."""

import logging
from typing import Any

import anyio
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
# one, queries fail with HTTP 400 ("Index required but not found") on instances
# that enforce strict-mode index-required filtering (Qdrant Cloud, network mode
# with strict settings). The three string fields (doc_id, user_id, doc_type)
# carry str values after producer normalization, so KEYWORD is the correct
# schema; is_placeholder is the bool used by ``get_placeholder_filter`` and
# ``delete_placeholder_point`` (see vector/placeholder.py), so it gets BOOL.
_PAYLOAD_INDEX_FIELDS: dict[str, PayloadSchemaType] = {
    "doc_id": PayloadSchemaType.KEYWORD,
    "user_id": PayloadSchemaType.KEYWORD,
    "doc_type": PayloadSchemaType.KEYWORD,
    "is_placeholder": PayloadSchemaType.BOOL,
}

# Sentinel point that records "this collection has been backfilled to str
# doc_id". Written after a successful pass of _backfill_doc_id_to_string so
# subsequent restarts can short-circuit the O(N) scroll. Carries no
# user_id/doc_id/doc_type, so production search filters (which always
# require user_id) never see it. In :memory: mode the sentinel does not
# survive a restart — the scroll runs every start, but is a no-op against
# an empty in-memory collection.
_DOC_ID_BACKFILL_SENTINEL_ID: str = "00000000-0000-0000-0000-d0c1d0d1d0c1"
_DOC_ID_BACKFILL_SENTINEL_PAYLOAD: dict[str, str] = {"_migration_marker": "doc_id_v1"}

# Singleton instance + init lock. The lock serialises concurrent first
# callers so the idempotent-but-expensive startup migration
# (``_backfill_doc_id_to_string`` + ``_ensure_payload_indexes``) only runs
# once per process. Steady-state callers hit the fast path above the lock
# and never acquire it.
_qdrant_client: AsyncQdrantClient | None = None
_qdrant_init_lock: anyio.Lock = anyio.Lock()


async def _ensure_payload_indexes(
    client: AsyncQdrantClient,
    collection_name: str,
    existing_schema: dict[str, Any] | None = None,
) -> None:
    """Create payload indexes for fields used in exact-match filters.

    Each entry in ``_PAYLOAD_INDEX_FIELDS`` is created with its declared
    schema type (KEYWORD for string fields, BOOL for ``is_placeholder``).
    Skips fields that are already in ``existing_schema`` so routine
    restarts make no Qdrant write round-trips and emit no INFO log lines.
    Schema conflicts (a pre-existing index with a different type) still
    surface as a 400 — log loudly so operators can intervene, but keep
    going so the remaining fields still get indexed.

    Args:
        client: Qdrant client instance.
        collection_name: Target collection.
        existing_schema: The collection's current ``payload_schema``. If
            ``None``, this function fetches it via ``get_collection``;
            callers that have already fetched the collection info (e.g.
            ``get_qdrant_client``'s dimension-validation step) should pass
            it through to avoid a duplicate round-trip.
    """
    # Mirror the broad swallow in `_backfill_doc_id_to_string`: the singleton
    # in `get_qdrant_client` is already assigned by the time this function
    # runs, so a transient `get_collection` failure (timeout, DNS blip)
    # propagating out would leave the process holding a usable client with
    # the migration silently skipped on every subsequent call. Log ERROR
    # with exc_info and return; the next process restart retries from scratch.
    if existing_schema is None:
        try:
            collection_info = await client.get_collection(collection_name)
        except Exception:
            logger.error(
                "Failed to fetch collection info for '%s'; payload indexes not "
                "created. Will retry on next restart.",
                collection_name,
                exc_info=True,
            )
            return
        existing_schema = collection_info.payload_schema or {}
    failed_fields: list[str] = []

    for field, schema_type in _PAYLOAD_INDEX_FIELDS.items():
        if field in existing_schema:
            # Index already present — silent skip. Logging here on every
            # restart would be noise that hides the genuinely interesting
            # "first-time creation" line below.
            continue
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=schema_type,
                wait=True,
            )
            logger.info("Created %s payload index on '%s'", schema_type.name, field)
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


def _group_int_doc_ids(points: list[Any]) -> tuple[dict[str, list[Any]], int]:
    """Group point IDs whose payload carries an int doc_id, keyed by str(doc_id).

    Returns ``(by_value, scanned)`` where ``scanned`` is the total number of
    points inspected (str / missing payloads count toward scanned but are not
    grouped). Pulled out of ``_backfill_doc_id_to_string`` to keep that
    function's cognitive complexity within the project's limit.

    Point IDs widen to ``Any`` to satisfy the qdrant client's
    ``PointsSelector`` signature (UUID / int / str unions) without re-spelling
    the full type union here.
    """
    by_value: dict[str, list[Any]] = {}
    scanned = 0
    for point in points:
        scanned += 1
        # Qdrant client typing allows None payload even when with_payload was
        # requested; defensive default so the type checker is happy.
        payload = point.payload or {}
        value = payload.get("doc_id")
        if value is None or isinstance(value, str):
            continue
        if not isinstance(value, int):
            # Producers only ever write int or str; anything else is a
            # producer bug. Stringifying e.g. a float would write "3.0",
            # which producers (str(int)) and the keyword index would
            # never match, and which int() on the verification side
            # would later reject. Skip and log loudly instead.
            logger.warning(
                "Unexpected doc_id type %s on point %s; skipping rewrite",
                type(value).__name__,
                point.id,
            )
            continue
        by_value.setdefault(str(value), []).append(point.id)
    return by_value, scanned


async def _apply_backfill_writes(
    client: AsyncQdrantClient,
    collection_name: str,
    by_value: dict[str, list[Any]],
) -> int:
    """Apply one ``set_payload`` per stringified doc_id; return rewritten count.

    ``wait=True`` is load-bearing for two reasons:

    1. It ensures each batch commits before the scroll loop advances to
       the next page (and before the sentinel is written by the caller
       after ``_backfill_doc_id_to_string`` returns). A crash mid-scroll
       leaves no sentinel, so the next restart re-scrolls — and that
       re-scroll only sees a deterministic, committed partial state when
       each batch was committed synchronously. Fire-and-forget writes
       would race the next scroll page against still-in-flight rewrites.
    2. ``_ensure_payload_indexes`` runs after this backfill returns and
       can only index already-committed payload values. Without
       ``wait=True``, the keyword index could be built over points whose
       payloads are still int values in flight to disk, leaving them
       silently invisible to ``FieldCondition`` filters.
    """
    rewritten = 0
    for str_val, point_ids in by_value.items():
        await client.set_payload(
            collection_name=collection_name,
            payload={"doc_id": str_val},
            points=point_ids,
            wait=True,
        )
        rewritten += len(point_ids)
    return rewritten


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

    Only called for **existing** collections (see the
    ``if collection_name in collection_names`` branch in
    ``get_qdrant_client``); brand-new collections skip the backfill since
    there can be no legacy int payloads in a freshly created collection.

    Args:
        client: Qdrant client instance.
        collection_name: Target collection.
        dimension: Dense-vector dimension for the sentinel point's vector,
            forwarded by ``get_qdrant_client`` from the embedding model.
            Required because the sentinel is upserted into an existing
            collection and must match the collection's vector schema.
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
            by_value, batch_scanned = _group_int_doc_ids(points)
            scanned += batch_scanned
            rewritten += await _apply_backfill_writes(client, collection_name, by_value)

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
    # convention (vector/placeholder.py). The dense vector uses a single
    # non-zero element instead of all zeros: cosine distance is undefined
    # for the zero vector and Qdrant Cloud's strict mode rejects zero-vector
    # upserts. The sentinel still never participates in a search (no
    # user_id / doc_id / doc_type payload to match), so the exact value
    # doesn't matter — it just has to be normalisable.
    # A failure here is non-fatal: the data is correct; only the short-circuit
    # marker is missing, so the next restart will re-scroll an already-clean
    # collection (idempotent zero-write) before retrying the upsert.
    sentinel_dense = [1e-9] + [0.0] * (dimension - 1)
    sentinel_point = PointStruct(
        id=_DOC_ID_BACKFILL_SENTINEL_ID,
        vector={
            "dense": sentinel_dense,
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

    if rewritten:
        logger.info(
            "doc_id backfill complete on '%s': rewrote %d/%d int payloads to str",
            collection_name,
            rewritten,
            scanned,
        )
    else:
        logger.info(
            "doc_id backfill complete on '%s': %d points scanned, none required "
            "rewriting (collection already in str form)",
            collection_name,
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

    # Fast path: already initialized — skip lock acquisition for the
    # steady-state hot path (every MCP tool call after first start).
    if _qdrant_client is not None:
        return _qdrant_client

    # Slow path: serialise concurrent first-callers so the idempotent-but-
    # expensive startup migration (``_backfill_doc_id_to_string`` +
    # ``_ensure_payload_indexes``) runs exactly once. Without this lock,
    # parallel cold-start callers would all enter the init block, run the
    # migration N times, and emit duplicate "skip-because-exists" warnings
    # from the index helper — annoying log noise but not data corruption.
    async with _qdrant_init_lock:
        # Double-checked: another waiter may have initialized while we
        # blocked on the lock.
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
                    logger.info(
                        f"Using Qdrant persistent mode: {settings.qdrant_location}"
                    )
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
                # index covers every point. Pass the already-fetched
                # collection_info.payload_schema through to avoid a redundant
                # get_collection round-trip on every restart.
                await _backfill_doc_id_to_string(
                    _qdrant_client, collection_name, expected_dimension
                )
                await _ensure_payload_indexes(
                    _qdrant_client,
                    collection_name,
                    existing_schema=collection_info.payload_schema or {},
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
                # Freshly created collection has no payload schema yet; pass {}
                # explicitly to skip the otherwise-redundant get_collection call.
                await _ensure_payload_indexes(
                    _qdrant_client, collection_name, existing_schema={}
                )

    # Lock released. ``_qdrant_client`` is guaranteed non-None here:
    # either the fast path returned earlier, the lock-protected branch
    # set it, or a sibling waiter set it before we got the lock.
    assert _qdrant_client is not None
    return _qdrant_client
