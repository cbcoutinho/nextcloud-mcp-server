"""Canonical JSON encoding shared across cross-implementation hashes.

The Astrolabe Cloud decomposition (design §2.3) fixes a single canonical JSON
encoding so hashes computed here match those computed independently by the
external embedding-gateway service. Any drift in separators, key ordering, or
unicode handling would break Qdrant point-ID idempotency and ACL-hash
compatibility.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """Encode ``obj`` to canonical JSON bytes.

    Deterministic across implementations: sorted keys, no inter-token
    whitespace, non-ASCII preserved (UTF-8). Consumers: Qdrant point IDs
    (vector/payload_keys.py) and ACL hashes (acl_hash.py).
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
