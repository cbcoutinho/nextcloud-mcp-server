"""`nc_semantic_search` builds `SemanticSearchResult` in TWO places.

The normal path constructs each row once. The `include_context=True` path then
*rebuilds* those rows from scratch to attach the expansion fields — and because
it is a fresh constructor call rather than a copy, any field it forgets silently
reverts to that field's default.

That is how `rerank_score` was lost for `rerank=True` + `include_context=True`:
the response said `reranked: true` while every row carried `rerank_score: null`.
The failure is invisible unless something asserts the two paths agree, because
each construction site looks correct in isolation.

These tests pin the *class* of bug rather than the one field, so the next
optional field added to the model is covered without anyone remembering to.
"""

import ast
import inspect
from pathlib import Path

import pytest

from nextcloud_mcp_server.models.semantic import SemanticSearchResult

pytestmark = pytest.mark.unit

# Fields that legitimately differ between the two sites: the expansion path
# exists precisely to populate these, and the normal path leaves them unset.
_CONTEXT_ONLY_FIELDS = {
    "has_context_expansion",
    "marked_text",
    "before_context",
    "after_context",
    "has_before_truncation",
    "has_after_truncation",
    # The expansion path replaces the excerpt with the expanded text.
    "excerpt",
}


def _semantic_result_call_kwargs() -> list[set[str]]:
    """Keyword names passed at every `SemanticSearchResult(...)` site in the
    semantic server module, parsed from source.

    Static analysis rather than execution: reaching the expansion path at
    runtime needs a live Qdrant, a Nextcloud client and a chunk-context fetch,
    and mocking all of that would test the mocks. The invariant here is
    structural — "these two constructor calls pass the same fields" — so the
    source is the honest thing to assert on.
    """
    from nextcloud_mcp_server.server import semantic as semantic_mod

    source = Path(inspect.getfile(semantic_mod)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls: list[set[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "SemanticSearchResult":
            calls.append({kw.arg for kw in node.keywords if kw.arg})
    return calls


def test_there_are_exactly_two_construction_sites():
    """If a third appears, this file's assumption needs revisiting rather than
    silently covering only two of them."""
    assert len(_semantic_result_call_kwargs()) == 2


def test_both_construction_sites_pass_the_same_non_context_fields():
    """The expansion path rebuilds rows from scratch, so it must carry across
    everything the normal path sets — otherwise the forgotten field reverts to
    its default and the response quietly contradicts itself."""
    first, second = _semantic_result_call_kwargs()
    normal, expanded = sorted((first, second), key=len)

    missing = (normal - expanded) - _CONTEXT_ONLY_FIELDS
    assert not missing, (
        "context expansion rebuilds SemanticSearchResult and would silently "
        f"reset these fields to their defaults: {sorted(missing)}"
    )


def test_rerank_score_specifically_survives_context_expansion():
    """The regression that motivated this file: a response reporting
    `reranked: true` whose rows all carried `rerank_score: null`."""
    for kwargs in _semantic_result_call_kwargs():
        assert "rerank_score" in kwargs


def test_optional_model_fields_are_not_forgotten_by_the_expansion_path():
    """Guards the next field added to the model, not just the last one lost.

    Any optional field on the model that the normal path populates must also be
    populated by the expansion path.
    """
    model_fields = set(SemanticSearchResult.model_fields)
    first, second = _semantic_result_call_kwargs()
    normal, expanded = sorted((first, second), key=len)

    carried = (normal & model_fields) - _CONTEXT_ONLY_FIELDS
    assert carried <= expanded, (
        "fields set on the normal path but dropped by context expansion: "
        f"{sorted(carried - expanded)}"
    )
