"""Canonical ACL hash (design §11) — a cross-implementation contract.

The external document-processor and the local processor inside this server both
write the ``acl_hash`` Qdrant payload independently; the query path
(``search/verification.py``) builds the matching accessible set. All three must
agree byte-for-byte, so the canonicalization, hash, and accessible-set rules are
pinned here and exercised by an identical ``tests/fixtures/acl_hash_corpus.json``
in both repos (§11.5).

Key invariants:
- ``principal_id`` is NFC-normalized; **case is preserved, not folded** (§11.2).
- ``BLAKE2b-128`` (``digest_size=16`` → 32 hex chars), NOT the 64-byte default
  (§11.3).
- Public-link shares are excluded by the caller — they are bearer secrets not
  bound to an identity and never reachable via authenticated MCP queries (§11.1).
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable

from nextcloud_mcp_server.canonical import canonical_json

PRINCIPAL_TYPES = frozenset({"user", "group", "public"})

# The world-readable principal, included in every requester's accessible set
# (§11.4) and written on any document Nextcloud marks world-readable.
PUBLIC_PRINCIPAL: tuple[str, str] = ("public", "public")


def compute_principal_hash(principal_type: str, principal_id: str) -> str:
    """Hash one ``(principal_type, principal_id)`` tuple (§11.2–11.3)."""
    if principal_type not in PRINCIPAL_TYPES:
        raise ValueError(
            f"principal_type must be one of {sorted(PRINCIPAL_TYPES)}; "
            f"got {principal_type!r}"
        )
    # principal_type is ASCII by construction; only the id is normalized.
    normalized_id = unicodedata.normalize("NFC", principal_id)
    canonical = canonical_json([principal_type, normalized_id])
    return hashlib.blake2b(canonical, digest_size=16).hexdigest()


def compute_acl_hash(share_set: Iterable[tuple[str, str]]) -> list[str]:
    """Per-principal hash array for a document's share-set (§11.2).

    One element per share-set entry, in input order (Qdrant treats the array
    with set-semantics, so order is irrelevant at query time). The caller must
    have already dropped public-link shares (§11.1).
    """
    return [compute_principal_hash(ptype, pid) for ptype, pid in share_set]


def accessible_hash_set(username: str, groups: Iterable[str] = ()) -> set[str]:
    """Accessible hash set for an authenticated requester (§11.4).

    Derived entirely from OIDC claims: the requester's own ``(user, username)``,
    each ``(group, <name>)`` they hold, and unconditionally ``(public, public)``.
    """
    hashes = {compute_principal_hash("user", username)}
    for group in groups:
        hashes.add(compute_principal_hash("group", group))
    hashes.add(compute_principal_hash(*PUBLIC_PRINCIPAL))
    return hashes
