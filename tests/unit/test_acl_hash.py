"""ACL hash spec tests (design §11) + cross-impl corpus.

The corpus fixture is checked into both repos; the processor runs a mirror of
``test_corpus`` against the same file. Divergence is caught in CI.
"""

import json
from pathlib import Path

import pytest

from nextcloud_mcp_server.acl_hash import (
    PUBLIC_PRINCIPAL,
    accessible_hash_set,
    compute_acl_hash,
    compute_principal_hash,
)

CORPUS = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "acl_hash_corpus.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize(
    "case", CORPUS["cases"], ids=[c["name"] for c in CORPUS["cases"]]
)
def test_corpus(case):
    share_set = [tuple(p) for p in case["share_set"]]
    assert compute_acl_hash(share_set) == case["expected"]


def test_blake2b_128_bit_length():
    assert len(compute_principal_hash("user", "alice")) == 32


def test_case_is_preserved_not_folded():
    assert compute_principal_hash("user", "Alice") != compute_principal_hash(
        "user", "alice"
    )


def test_nfc_normalization():
    # decomposed 'café' (e + U+0301) hashes the same as precomposed 'café'.
    assert compute_principal_hash("user", "café") == compute_principal_hash(
        "user", "café"
    )


def test_invalid_principal_type_rejected():
    with pytest.raises(ValueError, match="principal_type must be one of"):
        compute_principal_hash("link", "sometoken")


def test_accessible_set_always_includes_public():
    s = accessible_hash_set("alice", [])
    assert compute_principal_hash(*PUBLIC_PRINCIPAL) in s


def test_accessible_set_membership_matches_share():
    # A document shared with a group the requester holds is admitted.
    doc = compute_acl_hash([("group", "engineering")])
    accessible = accessible_hash_set("bob", ["engineering", "all-staff"])
    assert any(h in accessible for h in doc)


def test_accessible_set_excludes_unheld_group():
    doc = compute_acl_hash([("group", "finance")])
    accessible = accessible_hash_set("bob", ["engineering"])
    assert not any(h in accessible for h in doc)
