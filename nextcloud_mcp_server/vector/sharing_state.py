"""Tenant-wide content dedup + observed-access ACL state for the vector index.

A file shared across users (directly, or via a group folder shared to a group)
has one Nextcloud ``fileid`` and one ``etag`` for everyone, and chunk point IDs
are user-agnostic (``uuid5(tenant_id, doc_id, chunk_index)`` — see
``vector/payload_keys.py``). So two users indexing the same file produce the
*same* points. The per-user freshness gate (filtered by ``user_id``) nonetheless
made them re-parse + re-embed the identical content on every scan (note 386945,
finding #5). This module lets the pipeline detect "already indexed by someone in
this tenant" and skip the expensive work.

Visibility is handled by an *observed-access* model rather than push-enumeration
of share/group-folder grants (which the server cannot read without admin creds —
group membership and the GroupFolders API are admin-only, and WebDAV PROPFIND
carries no ACL). The per-user scanner crawl is itself the access oracle: a tagged
file appears in a user's ``find_files_by_tag`` REPORT **iff** that user can read
it. So each point carries ``acl_principals`` — the set of ``user:<uid>`` whose
scanner has observed (hence can access) the file. The search filter ORs a
``MatchAny(acl_principals, ["user:<me>"])`` branch, and ``_verify_files`` (the
verify-on-read gate) re-checks each result against the user's tagged REPORT, so
an over-broad principal match can never leak content.

All point IDs are user-agnostic, so deletion must *release one user* (drop their
principal) and only remove the points when the principal set empties — otherwise
one user untagging a shared file would evict it for everyone still reading it.
"""

from __future__ import annotations

import logging

from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.collection_metadata import build_embedding_identity
from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

ACL_PRINCIPALS_KEY = "acl_principals"


def user_principal(user_id: str) -> str:
    """The ``acl_principals`` entry representing a single user's read access."""
    return f"user:{user_id}"


def file_title_from_path(file_path: str) -> str:
    """Human-facing title for an indexed file: its Nextcloud filename.

    We deliberately favour the filename over any embedded document title (e.g. a
    PDF's ``/Title`` metadata), which frequently disagrees with how the user
    named the file in Nextcloud and is confusing in the search/viz UI.
    """
    return file_path.rstrip("/").rsplit("/", 1)[-1] or file_path


def _document_filter(doc_id: str, doc_type: str, *, real_only: bool) -> Filter:
    """Match every chunk of one document; optionally exclude placeholder points."""
    must: list = [
        FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
        FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
    ]
    if real_only:
        must.append(get_placeholder_filter())
    return Filter(must=must)


async def find_indexed_content(
    doc_id: str,
    doc_type: str,
    etag: str,
    embedding_identity: str,
    index_mode: str = payload_keys.INDEX_MODE_HYBRID,
) -> dict | None:
    """Return a real point's payload if this exact content is already indexed.

    Looks tenant-wide (no ``user_id`` filter) for a non-placeholder point with
    the given ``doc_id``/``doc_type``/``etag``. The match is gated on
    ``embedding_identity`` in Python (not the Qdrant filter, to avoid requiring an
    index on that field): since point IDs are model-agnostic, a model switch
    overwrites the same points, so all live points for a doc share one identity —
    a mismatch means the existing vectors were produced by a different model and
    must be re-embedded, so we report "not indexed".

    ``index_mode`` applies the monotonic keyword→hybrid rule so a mixed collection
    never re-embeds in a loop: a **hybrid** claim against an existing **keyword**
    (sparse-only) point reports "not indexed" so the doc is reprocessed and gains
    a dense vector (upgrade); a **keyword** claim against an existing **hybrid**
    point is a hit (hybrid ⊇ keyword — never downgrade/strip the dense vector
    while any user holds the hybrid tag). Same-mode is a hit as before.

    Returns the payload dict (including ``acl_principals``) on a hit, else None.
    """
    if not etag:
        return None
    qdrant_client = await get_qdrant_client()
    settings = get_settings()
    points, _ = await qdrant_client.scroll(
        collection_name=settings.get_collection_name(),
        scroll_filter=Filter(
            must=[
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
                FieldCondition(key="etag", match=MatchValue(value=etag)),
                get_placeholder_filter(),
            ]
        ),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return None
    payload = dict(points[0].payload or {})
    if payload.get(payload_keys.EMBEDDING_IDENTITY) != embedding_identity:
        # Existing vectors were produced by a different embedding model — a
        # re-embed is required, so this content is not reusable as-is.
        return None
    # Monotonic keyword→hybrid upgrade: a hybrid claim cannot reuse sparse-only
    # keyword points (they lack the dense vector), so force a reprocess. Every
    # other combination reuses the existing points (same mode, or a keyword claim
    # against hybrid points — hybrid satisfies keyword). Existing points written
    # before INDEX_MODE existed have no key; treat them as hybrid (the prior
    # dense+sparse default) so a hybrid claim still dedups against them.
    existing_mode = payload.get(payload_keys.INDEX_MODE, payload_keys.INDEX_MODE_HYBRID)
    if (
        index_mode == payload_keys.INDEX_MODE_HYBRID
        and existing_mode == payload_keys.INDEX_MODE_KEYWORD
    ):
        return None
    return payload


async def existing_principals(doc_id: str, doc_type: str) -> list[str]:
    """Return the ``acl_principals`` already recorded for a document (or []).

    Used when re-indexing after a content change (etag differs, so the dedup
    race-guard misses and the points are overwritten): seeding the new points
    with the prior principal set preserves visibility for readers who had
    already claimed the file, instead of resetting it to just the indexer.
    """
    qdrant_client = await get_qdrant_client()
    settings = get_settings()
    points, _ = await qdrant_client.scroll(
        collection_name=settings.get_collection_name(),
        scroll_filter=_document_filter(doc_id, doc_type, real_only=True),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return []
    return list(dict(points[0].payload or {}).get(ACL_PRINCIPALS_KEY) or [])


async def add_principal(
    doc_id: str,
    doc_type: str,
    user_id: str,
    current_principals: list[str] | None,
) -> bool:
    """Record that ``user_id`` can read this document (observed-access ACL).

    No-op (returns False) when the user's principal is already present — so the
    steady state of a repeat scan writes nothing. Otherwise unions the principal
    onto every real chunk of the document via a single ``set_payload`` and
    returns True. Concurrent adds race to a last-writer-wins union; a dropped
    add is re-applied on the losing user's next scan, and verify-on-read gates
    correctness in the meantime.
    """
    principal = user_principal(user_id)
    existing = current_principals or []
    if principal in existing:
        return False
    new_principals = sorted(set(existing) | {principal})
    qdrant_client = await get_qdrant_client()
    settings = get_settings()
    await qdrant_client.set_payload(
        collection_name=settings.get_collection_name(),
        payload={ACL_PRINCIPALS_KEY: new_principals},
        points=_document_filter(doc_id, doc_type, real_only=True),
        wait=True,
    )
    logger.debug(
        "Granted read principal %s on %s_%s (now %d principal(s))",
        principal,
        doc_type,
        doc_id,
        len(new_principals),
    )
    return True


async def reconcile_document_path(
    doc_id: str,
    doc_type: str,
    stored_path: str | None,
    current_path: str,
) -> bool:
    """Refresh ``file_path``/``title`` on a renamed/moved file's existing points.

    A rename in Nextcloud keeps the ``fileid`` (our ``doc_id``) but changes the
    path while leaving content — hence ``etag`` and ``mtime`` — untouched, so
    both the dedup claim and the scanner's freshness gate skip re-embedding and
    the stored payload keeps the OLD path and OLD filename-derived title. This
    rewrites ``file_path`` and the derived ``title`` on every real chunk via a
    single metadata-only ``set_payload`` (no re-fetch, no re-embed).

    Returns False (no write attempted) only when the path is unchanged or empty.
    When the path differs it returns True after issuing the ``set_payload``; that
    write is itself a Qdrant-side no-op if no real chunks exist yet (e.g. only a
    placeholder), which the callers tolerate. A legacy point with no stored
    ``file_path`` is treated as changed, backfilling both fields.
    """
    if not current_path or stored_path == current_path:
        return False
    qdrant_client = await get_qdrant_client()
    settings = get_settings()
    await qdrant_client.set_payload(
        collection_name=settings.get_collection_name(),
        payload={
            "file_path": current_path,
            "title": file_title_from_path(current_path),
        },
        points=_document_filter(doc_id, doc_type, real_only=True),
        wait=True,
    )
    logger.info(
        "Reconciled path for %s_%s after rename/move: %r -> %r",
        doc_type,
        doc_id,
        stored_path,
        current_path,
    )
    return True


async def claim_existing_index(
    doc_id: str,
    doc_type: str,
    etag: str,
    user_id: str,
    index_mode: str = payload_keys.INDEX_MODE_HYBRID,
    current_path: str | None = None,
) -> bool:
    """Tenant-wide dedup claim: skip reprocessing if content is already indexed.

    Returns True when a non-placeholder point for this exact content (fileid +
    etag + current embedding model) already exists for some user in the tenant —
    in which case ``user_id`` is added to ``acl_principals`` (so the file remains
    searchable for them) and the caller should skip fetch/parse/embed. Returns
    False when nothing reusable exists and the document must be processed.

    When ``current_path`` is given (files), a dedup hit also reconciles a stale
    ``file_path``/``title`` on the existing points: identical content (etag) at a
    new path means the file was renamed/moved, which the dedup would otherwise
    silently skip. Reuses the payload already fetched here, so it adds no extra
    Qdrant round-trip in the steady (unchanged-path) state.

    Fail-safe: a Qdrant error during the lookup degrades to False (process the
    document normally) rather than aborting the scan — the dedup is an
    optimisation, never a correctness gate. A failure to record the principal
    after a confirmed hit is non-fatal (logged, not raised): verify-on-read still
    gates access and the user's next scan re-claims it.
    """
    # Must match what the chunk-point WRITER stamps (processor.py) via the shared
    # helper so the dedup comparison agrees (Deck #509). The identity is the
    # embedding model; the keyword-vs-hybrid mode is applied separately by
    # ``index_mode`` (monotonic keyword→hybrid — see find_indexed_content).
    embedding_identity = build_embedding_identity(get_settings())
    try:
        existing = await find_indexed_content(
            doc_id, doc_type, etag, embedding_identity, index_mode
        )
    except Exception as exc:  # noqa: BLE001 — degrade to "process normally"
        logger.warning(
            "Dedup lookup failed for %s_%s (%s); processing without dedup",
            doc_type,
            doc_id,
            exc,
        )
        return False
    if existing is None:
        return False
    if current_path:
        try:
            await reconcile_document_path(
                doc_id, doc_type, existing.get("file_path"), current_path
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal; retried next scan
            logger.warning(
                "Path reconcile failed for %s_%s (%s); next scan retries",
                doc_type,
                doc_id,
                exc,
            )
    try:
        await add_principal(doc_id, doc_type, user_id, existing.get(ACL_PRINCIPALS_KEY))
    except Exception as exc:  # noqa: BLE001 — non-fatal; recovered on next scan
        logger.warning(
            "Failed to grant read principal user:%s on %s_%s (%s); "
            "verify-on-read and the next scan will reconcile",
            user_id,
            doc_type,
            doc_id,
            exc,
        )
    return True


async def release_document_for_user(
    doc_id: str,
    doc_type: str,
    user_id: str,
) -> None:
    """Drop ``user_id``'s access to a document; delete points only when orphaned.

    Replaces a blind per-document delete. Because point IDs are user-agnostic, a
    shared document has one point set referenced by multiple principals; removing
    one user must not evict it for the others. Removes the user's principal and
    deletes the points only once no principal remains.

    Legacy points written before ``acl_principals`` existed have no principal
    set; for those we preserve the original behaviour (delete by
    ``user_id``/``doc_id``/``doc_type``) so a single-owner delete still works.
    """
    qdrant_client = await get_qdrant_client()
    settings = get_settings()
    collection = settings.get_collection_name()

    points, _ = await qdrant_client.scroll(
        collection_name=collection,
        scroll_filter=_document_filter(doc_id, doc_type, real_only=True),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    principals = (
        (dict(points[0].payload or {}).get(ACL_PRINCIPALS_KEY)) if points else None
    )

    if not principals:
        # No real points, or legacy points without a principal set: fall back to
        # the original per-user delete (also clears this user's placeholder).
        await qdrant_client.delete(
            collection_name=collection,
            points_selector=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
                ]
            ),
        )
        return

    remaining = sorted(p for p in principals if p != user_principal(user_id))
    if not remaining:
        # Last reader released — remove every point (real + placeholder).
        await qdrant_client.delete(
            collection_name=collection,
            points_selector=_document_filter(doc_id, doc_type, real_only=False),
        )
        logger.info(
            "Released last principal for %s_%s — document removed from index",
            doc_type,
            doc_id,
        )
    else:
        await qdrant_client.set_payload(
            collection_name=collection,
            payload={ACL_PRINCIPALS_KEY: remaining},
            points=_document_filter(doc_id, doc_type, real_only=True),
            wait=True,
        )
        logger.info(
            "Released principal %s for %s_%s — %d reader(s) remain",
            user_principal(user_id),
            doc_type,
            doc_id,
            len(remaining),
        )
