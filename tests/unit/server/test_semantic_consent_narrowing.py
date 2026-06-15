"""Unit tests for the search-side consent narrowing in nc_semantic_search.

The narrowing logic (intersect requested doc_types with the admin allow-set,
or restrict to the allow-set when none requested) is extracted into
``_consent_narrowed_doc_types`` so it can be tested without exercising the full
search path. ``allowed is None`` (no restriction / fail-open) is handled by the
caller skipping this helper entirely.
"""

from __future__ import annotations

import pytest

from nextcloud_mcp_server.server.semantic import _consent_narrowed_doc_types

pytestmark = pytest.mark.unit


def test_none_request_restricts_to_allow_set():
    # No explicit doc_types -> search exactly the allowed set (sorted).
    assert _consent_narrowed_doc_types(None, frozenset({"file", "note"})) == [
        "file",
        "note",
    ]


def test_request_intersected_with_allow_set_preserves_order():
    result = _consent_narrowed_doc_types(
        ["deck_card", "note", "file"], frozenset({"note", "file"})
    )
    assert result == ["note", "file"]


def test_disjoint_request_yields_empty():
    # Caller short-circuits to an empty response on [].
    assert _consent_narrowed_doc_types(["deck_card"], frozenset({"note"})) == []


def test_empty_allow_set_blocks_all():
    assert _consent_narrowed_doc_types(None, frozenset()) == []
    assert _consent_narrowed_doc_types(["note"], frozenset()) == []
