"""ADR-042 §4: text reads never see image points.

Image points are ``doc_type="file"`` points of the same document, marked
``vector_kind="image"``, with no chunk text. A text consumer that forgot to
exclude them would silently return an image as an empty-text chunk. So every
Qdrant read in the package must either build its filter with the helper that
excludes them, or be allowlisted here with the reason it must (or harmlessly
may) reach image points. The walk fails closed: a new call site fails this test
until someone decides which it is.
"""

import ast
import pathlib
import uuid

import pytest
from qdrant_client import AsyncQdrantClient, models

from nextcloud_mcp_server.search.access_filter import build_base_filter_conditions
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.placeholder import exclude_image_points

pytestmark = pytest.mark.unit

PACKAGE = pathlib.Path(__file__).parents[3] / "nextcloud_mcp_server"

# Qdrant read methods. ``count`` and ``search`` are common names, so they only
# count as Qdrant calls when they pass ``collection_name=``.
_ALWAYS = {
    "scroll",
    "query_points",
    "query_points_groups",
    "query_batch_points",
    "search_batch",
    "recommend",
    "facet",
}
_WITH_COLLECTION = {"count", "search"}
_HELPERS = ("exclude_image_points", "build_base_filter_conditions")

# "<path relative to the package>::<function>" -> why image points are fine.
ALLOWED = {
    "search/algorithms.py::get_indexed_doc_types": (
        "distinct doc types; an image point is a 'file' like its text points"
    ),
    "search/bm25_hybrid.py::_run_qdrant_query": (
        "query_filter is built by the caller with build_base_filter_conditions"
    ),
    "vector/dead_letter.py::is_dead_lettered": "document-scoped marker lookup",
    "vector/metrics_publisher.py::count_dead_lettered": "dead-letter tombstones",
    "vector/metrics_publisher.py::count_images._count": (
        "counts image points on purpose"
    ),
    "vector/metrics_publisher.py::compute_chunk_density_snapshot": (
        "matches chunk_index == 0, which an image point never carries"
    ),
    "vector/placeholder.py::query_document_metadata._first": (
        "'is it indexed?' must see a document whose only point is its image"
    ),
    "vector/placeholder.py::sweep_orphan_placeholders": "placeholders only",
    "vector/purge.py::purge_doc_types": "document-scoped purge",
    "vector/qdrant_client.py::_backfill_doc_id_to_string": "migration of every point",
    "vector/scanner.py::_iter_all_points": (
        "the scanner must see a document whose only point is its image"
    ),
    "vector/sharing_state.py::find_indexed_content": (
        "freshness check; the image point carries the full document payload"
    ),
    "vector/sharing_state.py::existing_principals": "document-scoped ACL state",
    "vector/sharing_state.py::release_document_for_user": (
        "ACL release must update the image point too"
    ),
}


def _qdrant_reads() -> dict[str, list[int]]:
    """Map "path::function" to the line numbers of its Qdrant reads."""
    found: dict[str, list[int]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text()
        rel = path.relative_to(PACKAGE).as_posix()
        stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                func = node.func
                if isinstance(func, ast.Attribute):
                    kwargs = {k.arg for k in node.keywords}
                    if func.attr in _ALWAYS or (
                        func.attr in _WITH_COLLECTION and "collection_name" in kwargs
                    ):
                        key = f"{rel}::{'.'.join(stack) or '<module>'}"
                        found.setdefault(key, []).append(node.lineno)
                self.generic_visit(node)

        Visitor().visit(ast.parse(source))
    return found


def _function_source(key: str) -> str:
    rel, qualname = key.split("::")
    source = (PACKAGE / rel).read_text()
    node: ast.AST = ast.parse(source)
    for name in qualname.split("."):
        node = next(
            n
            for n in ast.walk(node)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
        )
    return ast.get_source_segment(source, node) or ""


def test_every_qdrant_read_excludes_image_points_or_says_why():
    reads = _qdrant_reads()
    assert len(reads) > 15, "the walk found too few call sites; is it broken?"
    missing = [
        f"{key} (lines {lines})"
        for key, lines in reads.items()
        if key not in ALLOWED
        and not any(helper in _function_source(key) for helper in _HELPERS)
    ]
    assert not missing, (
        "These Qdrant reads neither exclude image points (exclude_image_points / "
        "build_base_filter_conditions) nor are allowlisted in ALLOWED:\n"
        + "\n".join(missing)
    )


def test_allowlist_has_no_stale_entries():
    stale = set(ALLOWED) - set(_qdrant_reads())
    assert not stale, f"ALLOWED names call sites that no longer exist: {stale}"


def test_base_conditions_exclude_images_unless_asked():
    def has_exclusion(conditions) -> bool:
        return exclude_image_points() in conditions

    assert has_exclusion(build_base_filter_conditions("alice"))
    assert not has_exclusion(
        build_base_filter_conditions("alice", include_image_points=True)
    )


async def test_must_not_keeps_text_points_and_drops_image_points():
    """Text points lack vector_kind; must_not on a match must keep them."""
    client = AsyncQdrantClient(":memory:")
    await client.create_collection(
        "c",
        vectors_config={
            "dense": models.VectorParams(size=2, distance=models.Distance.COSINE)
        },
    )
    await client.upsert(
        "c",
        points=[
            models.PointStruct(id=1, vector={"dense": [1.0, 0.0]}, payload={}),
            models.PointStruct(
                id=2,
                vector={"dense": [1.0, 0.0]},
                payload={payload_keys.VECTOR_KIND: payload_keys.VECTOR_KIND_IMAGE},
            ),
        ],
    )
    points, _ = await client.scroll(
        "c", scroll_filter=models.Filter(must=[exclude_image_points()])
    )
    assert [p.id for p in points] == [1]
    await client.close()


def test_image_point_id_is_stable_and_never_a_chunk_id():
    image_id = payload_keys.image_point_id("42")
    assert image_id == payload_keys.image_point_id("42")
    assert image_id != payload_keys.image_point_id("43")
    # The chunk ids the ingest worker actually writes (vector/processor.py).
    chunk_ids = {
        str(uuid.uuid5(uuid.NAMESPACE_DNS, f"file:42:chunk:{i}")) for i in range(1000)
    }
    assert image_id not in chunk_ids
