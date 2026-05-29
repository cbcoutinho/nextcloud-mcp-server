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


# Payload fields filtered by exact-match in scanner/processor/placeholder/eviction
# and the chunk-context lookup path. Qdrant requires a payload index for any
# field used in a FieldCondition; without one, queries fail with HTTP 400
# ("Index required but not found") on instances that enforce strict-mode
# index-required filtering (Qdrant Cloud, network mode with strict settings).
# The three string fields (doc_id, user_id, doc_type) carry str values after
# producer normalization, so KEYWORD is the correct schema. is_placeholder is
# the bool used by ``get_placeholder_filter`` and ``delete_placeholder_point``
# (see vector/placeholder.py), so it gets BOOL. chunk_index is the int used by
# ``_get_chunk_by_index_from_qdrant`` and ``get_chunk_bbox_and_page_from_qdrant``
# (see search/context.py) — the always-indexed fast path that the offset-based
# fallback exists to avoid; it has to actually be indexed for that promise to
# hold on Qdrant Cloud strict mode. chunk_start_offset / chunk_end_offset are
# the ints used by the legacy offset fallback in the same module — pre-#75
# clients have no chunk_index payload, so the offset path still has to work
# (or 400 silently and return None on Qdrant Cloud strict mode).
_PAYLOAD_INDEX_FIELDS: dict[str, PayloadSchemaType] = {
    "doc_id": PayloadSchemaType.KEYWORD,
    "user_id": PayloadSchemaType.KEYWORD,
    # owner_id is the ACL-aware filter field: every search applies
    # MatchAny(key="owner_id", any=accessible_owners) (see
    # search/access_filter.py). Without a keyword index Qdrant full-scans the
    # collection to evaluate it — invisible at small scale, but a latency
    # regression at tens of thousands of points and an HTTP 400 on Qdrant
    # Cloud strict payload-validation mode. Mirrors the user_id treatment;
    # _ensure_payload_indexes is idempotent so existing collections migrate
    # at startup without operator intervention.
    "owner_id": PayloadSchemaType.KEYWORD,
    "doc_type": PayloadSchemaType.KEYWORD,
    "is_placeholder": PayloadSchemaType.BOOL,
    "chunk_index": PayloadSchemaType.INTEGER,
    "chunk_start_offset": PayloadSchemaType.INTEGER,
    "chunk_end_offset": PayloadSchemaType.INTEGER,
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
# and never acquire it. The lock is lazy-initialised inside
# ``get_qdrant_client`` rather than constructed at module import time:
# anyio's docs are explicit that synchronization primitives should be
# instantiated within an async context, and ``anyio_mode = "auto"`` in
# pyproject.toml means tests can run under trio where eager construction
# would fail. Construction is safe under cooperative multitasking — there
# is no ``await`` between the None-check and the assignment, so two
# coroutines cannot both create a lock.
_qdrant_client: AsyncQdrantClient | None = None
_qdrant_init_lock: anyio.Lock | None = None


async def _create_one_payload_index(
    client: AsyncQdrantClient,
    collection_name: str,
    field: str,
    schema_type: PayloadSchemaType,
) -> bool:
    """Create one payload index with per-field error containment.

    Returns True on success or benign 400 schema-conflict (caller treats as
    indexed). Returns False if the field should be added to the caller's
    failed-fields list. Never re-raises: the singleton in
    ``get_qdrant_client`` is already assigned by the time this runs, so
    propagating a network blip would leave the process holding a usable
    client with the migration silently incomplete.
    """
    try:
        await client.create_payload_index(
            collection_name=collection_name,
            field_name=field,
            field_schema=schema_type,
            wait=True,
        )
        logger.info("Created %s payload index on '%s'", schema_type.name, field)
        return True
    except UnexpectedResponse as e:
        body = getattr(e, "content", b"") or b""
        body_text = body.decode("utf-8", errors="replace")
        # 400 is the expected schema-conflict path (index already exists
        # with a different type). Verified for Qdrant OSS, where an
        # idempotent re-create against a matching schema returns 200; if
        # Qdrant Cloud diverges and returns 400 for benign re-creates,
        # the WARNING below will fire on every restart against an
        # already-indexed collection — read the response body before
        # treating that as a real schema conflict. 5xx is unexpected —
        # keep the loop going so the remaining fields still get
        # attempted, but log at error so operators see it.
        if e.status_code == 400:
            logger.warning(
                "Schema conflict on payload index '%s': %s", field, body_text
            )
            # Treat schema conflict the same as a wrong-type index
            # discovered via `existing_schema` in `_ensure_payload_indexes`
            # (lines 195-206) — both are "the index present is not the
            # one we'd build", so the consolidated `Payload index
            # creation incomplete` summary should fire in both cases.
            # Without this, tenants whose payload_schema is hidden from
            # their JWT (Qdrant Cloud collection-scoped tokens) would
            # only see the per-field WARNING and miss the summary.
            return False
        logger.error(
            "Unexpected error creating payload index on '%s' (status %s): %s",
            field,
            e.status_code,
            body_text,
        )
        return False
    except Exception:
        # Raw network / timeout failures (httpx.ConnectError,
        # asyncio.TimeoutError, etc.) reach here — outside the HTTP-status
        # taxonomy that UnexpectedResponse covers. Same containment
        # rationale as above: one transient failure on one field must not
        # skip the rest, and the singleton in get_qdrant_client is already
        # assigned by this point so re-raising would leave the process
        # holding a usable client with the migration silently incomplete.
        logger.error(
            "Network error creating payload index on '%s'; "
            "field will remain unindexed until next successful restart",
            field,
            exc_info=True,
        )
        return False


async def _ensure_payload_indexes(
    client: AsyncQdrantClient,
    collection_name: str,
    existing_schema: dict[str, Any] | None = None,
) -> None:
    """Create payload indexes for fields used in exact-match filters.

    Each entry in ``_PAYLOAD_INDEX_FIELDS`` is created with its declared
    schema type (KEYWORD for string fields, BOOL for ``is_placeholder``,
    INTEGER for ``chunk_index``). Skips fields that are already in
    ``existing_schema`` so routine restarts make no Qdrant write round-trips
    and emit no INFO log lines. Per-field error handling (schema conflicts,
    network errors) lives in ``_create_one_payload_index``; this loop is
    flat so a single transient failure on one field does not skip the rest.

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
            # Index already present. Confirm the existing schema type matches
            # what we'd create — a pre-existing collection with `doc_id`
            # indexed as INTEGER (the bug this PR fixes) would otherwise
            # silently survive here, and searches using
            # MatchValue(value="123") would keep failing with HTTP 400 on
            # Qdrant Cloud strict mode. Compare via PayloadSchemaType
            # equality; PayloadIndexInfo.data_type is the same enum
            # we wrote with.
            existing_info = existing_schema[field]
            existing_type = getattr(existing_info, "data_type", None)
            if existing_type is not None and existing_type != schema_type:
                logger.warning(
                    "Payload index on '%s' has wrong schema type "
                    "(got %s, expected %s); searches filtering on this "
                    "field will fail with HTTP 400 until the index is "
                    "dropped and recreated. See docs/configuration.md "
                    "for the recovery procedure.",
                    field,
                    getattr(existing_type, "name", existing_type),
                    schema_type.name,
                )
                failed_fields.append(field)
            # Either way, skip the create call: a matching index needs no
            # work, and a mismatch must not be auto-repaired (operator
            # intervention only — see docs/configuration.md).
            continue
        if not await _create_one_payload_index(
            client, collection_name, field, schema_type
        ):
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
        # Strict type check: bool is a subclass of int in Python, so an
        # `isinstance(value, int)` guard would let `True`/`False` slip
        # through and be stringified to `"True"`/`"False"` — which the
        # keyword index would never match and the verification side
        # would later reject. Producers only ever write int or str;
        # anything else (bool, float, etc.) is a producer bug. Skip
        # and log loudly instead.
        if type(value) is not int:
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
    # Smaller than ``_DELETION_TRACKING_PAGE_SIZE = 1024`` in
    # ``vector/scanner.py`` because this is a read-write path: every batch
    # is followed by a ``set_payload`` upsert, and 256-point upserts are
    # the working size where Qdrant comfortably accepts writes without
    # timing out under load. The scanner-side scroll has no per-page write
    # round-trip, so it can use a larger page.
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
    global _qdrant_client, _qdrant_init_lock

    # Fast path: already initialized — skip lock acquisition for the
    # steady-state hot path (every MCP tool call after first start).
    if _qdrant_client is not None:
        return _qdrant_client

    # Lazy-create the init lock on first cold-start. Safe under cooperative
    # multitasking: there is no ``await`` between the None-check and the
    # assignment, so two coroutines cannot both reach the construction.
    # See the rationale on _qdrant_init_lock for why eager construction
    # would break under the trio backend.
    if _qdrant_init_lock is None:
        _qdrant_init_lock = anyio.Lock()

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

            # Build the client into a local ``provisional`` and only publish
            # it to the global ``_qdrant_client`` after the migration awaits
            # below have all completed. The fast-path check at the top of
            # this function reads ``_qdrant_client`` without the lock, so
            # publishing the constructed-but-unmigrated client would let a
            # concurrent caller short-circuit the lock and fire a filtered
            # search before ``_ensure_payload_indexes`` runs — that search
            # would 400 with "Index required but not found".
            provisional: AsyncQdrantClient

            # Detect mode and initialize client accordingly
            if settings.qdrant_url:
                # Network mode
                logger.info("Using Qdrant network mode: %s", settings.qdrant_url)
                provisional = AsyncQdrantClient(
                    url=settings.qdrant_url,
                    api_key=settings.qdrant_api_key,
                    timeout=30,
                )
            elif settings.qdrant_location:
                # Local mode (either :memory: or persistent path)
                if settings.qdrant_location == ":memory:":
                    logger.info("Using Qdrant in-memory mode: :memory:")
                    provisional = AsyncQdrantClient(":memory:")
                else:
                    # Persistent local mode - use path parameter
                    logger.info(
                        "Using Qdrant persistent mode: %s", settings.qdrant_location
                    )
                    provisional = AsyncQdrantClient(path=settings.qdrant_location)
            else:
                # Should not happen due to __post_init__ validation, but handle gracefully
                logger.warning("No Qdrant mode configured, defaulting to :memory:")
                provisional = AsyncQdrantClient(":memory:")

            # Get collection name (auto-generated from deployment ID + model)
            collection_name = settings.get_collection_name()

            embedding_service = get_embedding_service()

            # Detect dimension dynamically (for OllamaEmbeddingProvider)
            if hasattr(embedding_service.provider, "_detect_dimension"):
                await embedding_service.provider._detect_dimension()  # type: ignore[call-non-callable]

            expected_dimension = embedding_service.get_dimension()

            # Existence check folded into the get_collection() call.
            #
            # In managed multi-tenant Qdrant Cloud setups, per-tenant JWTs are
            # scoped to a single collection (`access: [{"collection": "...",
            # "access": "rw"}]`) and Qdrant denies the cluster-level meta
            # endpoints `GET /collections` (used by `get_collections()`) and
            # `GET /collections/{name}/exists` (used by `collection_exists()`)
            # with 403 Forbidden — by design, since listing or probing
            # collections cluster-wide is a tenant-isolation boundary.
            # `GET /collections/{name}` (the underlying call for
            # `get_collection()`) is the only existence-probe permitted on a
            # collection-scoped JWT — it returns 200 with the collection
            # detail on hit and 404 on miss.
            logger.debug("Fetching collection '%s' details...", collection_name)
            collection_info = None
            try:
                collection_info = await provisional.get_collection(collection_name)
            except UnexpectedResponse as exc:
                if exc.status_code != 404:
                    raise
                logger.debug("Collection '%s' not found (404).", collection_name)
            except ValueError as exc:
                # Local/in-memory qdrant_client raises ValueError(f"Collection
                # {name} not found") instead of UnexpectedResponse — see
                # qdrant_client/local/async_qdrant_local.py. Match on the
                # message rather than catching every ValueError so genuine
                # programming bugs (bad collection_name validation, etc.)
                # still propagate. PR #779 introduced this regression by
                # switching the existence probe from `collection_exists()`
                # (which returned a bool in both modes) to `get_collection`
                # without accounting for the local-mode signalling
                # convention; the failing single-user / login-flow /
                # multi-user-basic CI jobs all exercise this path.
                if "not found" not in str(exc):
                    raise
                logger.debug("Collection '%s' not found (local mode).", collection_name)

            if collection_info is not None:
                # Collection exists - validate dimensions
                logger.debug(
                    "Collection '%s' found, validating dimensions...", collection_name
                )
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
                    "Using existing Qdrant collection: %s (dimension=%s, model=%s)",
                    collection_name,
                    actual_dimension,
                    settings.get_embedding_model_name(),
                )

                # Existing collections may pre-date the doc_id normalization /
                # payload-index work. Backfill before creating the index so the
                # index covers every point. Pass the already-fetched
                # collection_info.payload_schema through to avoid a redundant
                # get_collection round-trip on every restart — safe because
                # _backfill_doc_id_to_string only rewrites payload *values*,
                # never schema or indexes, so the snapshot remains accurate
                # across the backfill call.
                await _backfill_doc_id_to_string(
                    provisional, collection_name, expected_dimension
                )
                await _ensure_payload_indexes(
                    provisional,
                    collection_name,
                    existing_schema=collection_info.payload_schema or {},
                )

            else:
                # Collection doesn't exist - create it
                embedding_model = settings.get_embedding_model_name()
                logger.info(
                    "Collection '%s' not found, creating with dimension=%s, model=%s...",
                    collection_name,
                    expected_dimension,
                    embedding_model,
                )
                await provisional.create_collection(
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
                    "Created Qdrant collection: %s\\n  Dense vector dimension: %s\\n  Dense embedding model: %s\\n  Sparse vectors: BM25 (for hybrid search)\\n  Distance: COSINE\\nBackground sync will index all documents with dense + sparse vectors.",
                    collection_name,
                    expected_dimension,
                    embedding_model,
                )
                # Freshly created collection has no payload schema yet; pass
                # {} explicitly to skip the otherwise-redundant
                # get_collection call. Every field in _PAYLOAD_INDEX_FIELDS
                # then goes through create_payload_index; on a brand-new
                # collection none of them exist yet, so the WARNING in the
                # 400-handler should *never* fire on this path. If it does
                # on Qdrant Cloud first-start, that points at a
                # deployment-level issue (race with a concurrent creator,
                # implicit auto-indexes, etc.) worth investigating before
                # suppressing.
                await _ensure_payload_indexes(
                    provisional, collection_name, existing_schema={}
                )

            # Publish only after the migration awaits completed. From this
            # point on, fast-path callers may short-circuit the lock and
            # use the client; every payload index they could filter on now
            # exists.
            _qdrant_client = provisional

    # Lock released. ``_qdrant_client`` is guaranteed non-None here:
    # either the fast path returned earlier, the lock-protected branch
    # set it, or a sibling waiter set it before we got the lock.
    assert _qdrant_client is not None
    return _qdrant_client
