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
