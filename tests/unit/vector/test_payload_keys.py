"""Drift guard for the cross-implementation point-ID namespace (design §2.2).

The MCP server and the external document-processor must compute identical chunk
point IDs. This test pins the NAMESPACE against a fixture checked into both
repos; the processor repo runs a mirror of this test against the same fixture.
"""

import uuid
from pathlib import Path

from nextcloud_mcp_server.vector import payload_keys

FIXTURE = Path(__file__).parents[2] / "fixtures" / "namespace_uuid.txt"


def test_namespace_matches_fixture():
    assert str(payload_keys.NAMESPACE) == FIXTURE.read_text().strip()


def test_point_id_deterministic():
    a = payload_keys.point_id("t", "d", 0)
    assert a == payload_keys.point_id("t", "d", 0)
    assert a != payload_keys.point_id("t", "d", 1)
    assert a != payload_keys.point_id("t2", "d", 0)


def test_point_id_exact_value():
    # Pins the canonical-JSON name + uuid5 formula so a refactor that changes
    # key ordering or encoding is caught (would silently break idempotency).
    expected = str(
        uuid.uuid5(
            payload_keys.NAMESPACE,
            '{"chunk_index":0,"doc_id":"d","tenant_id":"t"}',
        )
    )
    assert payload_keys.point_id("t", "d", 0) == expected


def test_payload_key_constants():
    assert payload_keys.EMBEDDING_IDENTITY == "embedding_identity"
    assert payload_keys.ACL_HASH == "acl_hash"
    assert payload_keys.PROCESSOR_VERSION == "processor_version"
    assert payload_keys.PARSED_AT == "parsed_at"
    assert payload_keys.PIPELINE_TIER == "pipeline_tier"
