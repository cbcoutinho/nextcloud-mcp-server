"""Qdrant payload-key constants + the shared point-ID namespace (design §2.2).

These names and the ``NAMESPACE`` UUID are a cross-implementation contract: the
external document-processor (astrolabe-cloud-website) computes identical chunk
point IDs and writes the same payload keys. Divergence in ``NAMESPACE`` would
break Qdrant upsert idempotency and duplicate chunks at the Phase 2 cutover, so
the literal is pinned by a fixture checked into both repos
(``tests/fixtures/namespace_uuid.txt``) and asserted equal in each repo's suite.

Even in the default local mode the MCP server writes these keys on upsert, so a
later migration to the external processor is friction-free (design §10.2).
"""

from __future__ import annotations

import uuid

from nextcloud_mcp_server.canonical import canonical_json

# Payload keys introduced by the decomposition (design §10.2).
EMBEDDING_IDENTITY = "embedding_identity"
ACL_HASH = "acl_hash"
PROCESSOR_VERSION = "processor_version"
PARSED_AT = "parsed_at"
PIPELINE_TIER = "pipeline_tier"

# Per-document index mode: "hybrid" (dense + BM25 sparse) or "keyword" (BM25
# sparse only). Drives whether a point carries a dense vector, gates verify-on-
# read to the right tag, and slices ingestion billing. See INDEX_MODE_HYBRID /
# INDEX_MODE_KEYWORD.
INDEX_MODE = "index_mode"
INDEX_MODE_HYBRID = "hybrid"
INDEX_MODE_KEYWORD = "keyword"

# Raw source size of the document in bytes at ingestion time
# (``ingested_byte_size``: raw WebDAV binary for files, UTF-8 text size for text
# doc types). Persisted on every chunk so the current-corpus chunk-density
# snapshot (``astrolabe_qdrant_chunk_density_chunks_per_mb_current``) can compute
# chunks-per-MB from live Qdrant state — the denominator the ingest-time density
# histogram consumes but that was previously discarded after embedding. Written
# forward-only: documents indexed before this key shipped carry no value and are
# reported via the ``uncovered_documents`` gauge until re-ingested.
SOURCE_BYTES = "source_bytes"

# Ancestor folder file-IDs of a file's canonical path (ADR-033 Phase 3, Deck
# #740), as a list of stringified Nextcloud fileids from the immediate parent up
# to the user root. A shared folder keeps ONE canonical fileid across every user
# who mounts it, so this term is user-agnostic for the shared portion — which is
# what makes the folder-scope filter both correct for every reader and flat in
# corpus size. Search resolves a ``path_prefix`` to its folder's fileid and
# applies MatchAny(folder_ancestors, [folder_id]) — a TRUE left-anchored
# containment (unlike the token/substring MatchText on file_path, card #739),
# evaluated inside HNSW traversal. Written forward-only for files; points indexed
# before this key carry no value and fall back to the file_path MatchText branch
# until an admin payload backfill populates them (no re-embed).
FOLDER_ANCESTORS = "folder_ancestors"

# Fixed platform namespace for deterministic chunk point IDs (design §2.2).
# Derived once from ``uuid5(NAMESPACE_DNS, "astrolabe.cloud/mcp/point-id/v1")``
# and pinned here as a literal so neither repo recomputes it. DO NOT CHANGE —
# see the module docstring for the cross-implementation contract.
NAMESPACE = uuid.UUID("b050b8ac-c6aa-5566-9584-506b39c1c096")


def point_id(tenant_id: str, doc_id: str, chunk_index: int) -> str:
    """Deterministic chunk point ID, identical across MCP server + processor.

    ``uuid5(NAMESPACE, canonical_json({...}))`` per design §2.2. The canonical
    JSON name (sorted keys, no whitespace) must match the processor
    byte-for-byte so re-indexing the same chunk upserts in place rather than
    duplicating.
    """
    name = canonical_json(
        {"tenant_id": tenant_id, "doc_id": doc_id, "chunk_index": chunk_index}
    ).decode("utf-8")
    return str(uuid.uuid5(NAMESPACE, name))
