"""Per-collection metadata: embedding identity + chunking config (design §10.1).

The query path needs to know which embedding produced a collection's vectors
(``embedding_identity``) and how it was chunked (``chunking_config``) so it can
request the matching embedding at lookup time. Two sources, selected by
``COLLECTION_METADATA_SOURCE``:

- ``qdrant`` — a sentinel point (deterministic UUID, normalisable non-zero dense
  vector) stored inside the collection. Works for any Qdrant deployment, so
  self-hosters benefit even without a control plane.
- ``api`` — an HTTP GET against the control plane
  (``/v1/qdrant-collections/{name}/metadata``).

On a missing/unreadable sentinel the query path logs a warning and falls back to
the environment-configured defaults — matching today's monolith behavior, so
query availability is preserved (design §10.1).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient, models

from ..config import Settings, get_settings
from .payload_keys import EMBEDDING_IDENTITY

logger = logging.getLogger(__name__)

# Deterministic sentinel point id (design §10.1). Carries collection metadata
# and never matches a search (no user_id/doc_id/doc_type payload to match).
# The nil UUID is used deliberately: real chunk point ids are UUID5s derived
# from (namespace, doc_id, chunk_index), so the all-zero id can never collide
# with a content point — and it is trivially recognisable in Qdrant's UI.
SENTINEL_POINT_ID = "00000000-0000-0000-0000-000000000000"

# Sentinel payload keys.
CHUNKING_CONFIG = "chunking_config"
IS_SENTINEL = "is_sentinel"


def build_embedding_identity(settings: Settings | None = None) -> str:
    """The embedding identity for locally-produced vectors — the single source of
    truth for the identity stamped on chunk points, the collection sentinel, the
    admin backfill, and the cross-user dedup comparison, so all four agree.

    It is the active dense embedding MODEL name: the gateway and query path route
    on it, matching the collection-name derivation, and a model switch changes it
    so the dedup lookup misses and forces a re-embed. It is orthogonal to
    keyword-vs-hybrid — keyword documents share this collection and carry the same
    model identity (they simply omit the dense vector), so both modes dedup in one
    identity space. The keyword/hybrid distinction is tracked separately by
    ``payload_keys.INDEX_MODE``. This MUST be produced identically everywhere the
    identity is written OR compared (Deck #509).
    """
    s = settings or get_settings()
    return s.get_embedding_model_name()


def env_default_metadata(settings: Settings | None = None) -> dict[str, Any]:
    """Metadata derived purely from environment config — the fallback when no
    sentinel/API metadata is available."""
    s = settings or get_settings()
    return {
        "embedding_identity": build_embedding_identity(s),
        "chunking_config": {
            "chunk_size": s.document_chunk_size,
            "chunk_overlap": s.document_chunk_overlap,
        },
    }


def _sentinel_dense(dimension: int) -> list[float]:
    # Cosine distance is undefined for the zero vector (and Qdrant Cloud strict
    # mode rejects it), so use one tiny non-zero element — mirrors the doc-id
    # backfill sentinel in qdrant_client.py.
    return [1e-9] + [0.0] * (dimension - 1)


async def upsert_sentinel(
    client: AsyncQdrantClient,
    collection_name: str,
    *,
    embedding_identity: str,
    chunking_config: dict[str, Any],
    dimension: int,
) -> None:
    """Idempotently write the metadata sentinel point for a collection."""
    point = models.PointStruct(
        id=SENTINEL_POINT_ID,
        vector={
            "dense": _sentinel_dense(dimension),
            "sparse": models.SparseVector(indices=[], values=[]),
        },
        payload={
            EMBEDDING_IDENTITY: embedding_identity,
            CHUNKING_CONFIG: chunking_config,
            IS_SENTINEL: True,
        },
    )
    await client.upsert(collection_name=collection_name, points=[point], wait=True)
    logger.debug("Upserted metadata sentinel on '%s'", collection_name)


async def _read_from_qdrant(
    client: AsyncQdrantClient, collection_name: str
) -> dict[str, Any] | None:
    points = await client.retrieve(
        collection_name=collection_name,
        ids=[SENTINEL_POINT_ID],
        with_payload=True,
    )
    if not points:
        return None
    payload = points[0].payload or {}
    if EMBEDDING_IDENTITY not in payload:
        return None
    return {
        "embedding_identity": payload.get(EMBEDDING_IDENTITY),
        "chunking_config": payload.get(CHUNKING_CONFIG),
    }


async def _read_from_api(
    api_url: str,
    collection_name: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """GET the control-plane metadata for a collection.

    ``client`` lets the caller pass a shared, lifespan-managed
    :class:`httpx.AsyncClient`; when omitted we create a short-lived one.

    Auth note: the control-plane metadata endpoint is currently unauthenticated
    (read-only collection identity, no tenant secrets), matching the gateway's
    present state. When the control plane gains M2M auth this should reuse the
    same OIDC credentials as ``GatewayTokenProvider`` (design §10.2).
    """
    url = f"{api_url.rstrip('/')}/v1/qdrant-collections/{collection_name}/metadata"

    async def _do(c: httpx.AsyncClient) -> dict[str, Any] | None:
        resp = await c.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    if client is not None:
        return await _do(client)
    # httpx verifies TLS by default (verify=True); stated here to be explicit.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0), verify=True
    ) as owned:
        return await _do(owned)


async def read_collection_metadata(
    client: AsyncQdrantClient,
    collection_name: str,
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Read collection metadata from the configured source, falling back to env
    defaults on any miss/error (preserves query availability — §10.1).

    ``http_client`` is forwarded to the ``api`` source so callers on the query
    path can share a lifespan-managed client instead of opening one per read.
    """
    s = settings or get_settings()
    meta: dict[str, Any] | None = None
    try:
        if s.collection_metadata_source == "api":
            # Defence-in-depth (robust under ``python -O``): __post_init__
            # guarantees the URL when COLLECTION_METADATA_SOURCE=api.
            if s.collection_metadata_api_url is None:
                raise ValueError(
                    "COLLECTION_METADATA_SOURCE=api requires "
                    "COLLECTION_METADATA_API_URL"
                )
            meta = await _read_from_api(
                s.collection_metadata_api_url, collection_name, client=http_client
            )
        else:
            meta = await _read_from_qdrant(client, collection_name)
    except Exception:
        logger.warning(
            "Collection metadata read failed for '%s' (source=%s); using env defaults",
            collection_name,
            s.collection_metadata_source,
        )

    if not meta or not meta.get("embedding_identity"):
        logger.warning(
            "Collection metadata missing for '%s' (source=%s); using env defaults",
            collection_name,
            s.collection_metadata_source,
        )
        return env_default_metadata(s)
    return meta
