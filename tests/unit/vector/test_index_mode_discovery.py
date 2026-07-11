"""Unit tests for per-document index-mode discovery (card #609).

Two Nextcloud tags feed one ingestion pipeline: ``vector_sync_tag`` →
hybrid (dense + BM25 sparse) and ``vector_sync_keyword_tag`` → keyword (BM25
sparse only). ``_discover_tagged_files`` unions both, stamping ``_index_mode``
on each file with **hybrid precedence** (a file carrying both tags is hybrid, a
superset of keyword). The keyword tag is only queried when configured.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.processor import _reconcile_tag_event
from nextcloud_mcp_server.vector.scanner import DocumentTask, _discover_tagged_files


def _settings(tag: str = "vector-index", keyword_tag: str = "") -> MagicMock:
    s = MagicMock()
    s.vector_sync_tag = tag
    s.vector_sync_keyword_tag = keyword_tag
    return s


def _client_for(tag_files: dict[str, list[dict]]) -> MagicMock:
    """A client whose find_files_by_tag returns per-tag file lists."""
    nc = MagicMock()

    def _find(tag_name, mime_type_filter=None):
        # Return a shallow copy so the helper's ``_index_mode`` stamping does not
        # mutate the fixture across calls. Plain sync side_effect — AsyncMock
        # wraps the return value in the awaitable the helper awaits.
        return [dict(f) for f in tag_files.get(tag_name, [])]

    nc.find_files_by_tag = AsyncMock(side_effect=_find)
    return nc


@pytest.mark.unit
def test_document_task_defaults_to_hybrid():
    """Every producer that doesn't set index_mode gets hybrid (unchanged behavior)."""
    task = DocumentTask(
        user_id="alice",
        doc_id="1",
        doc_type="note",
        operation="index",
        modified_at=0,
    )
    assert task.index_mode == payload_keys.INDEX_MODE_HYBRID


@pytest.mark.unit
async def test_keyword_tag_empty_queries_only_hybrid():
    """With the keyword tag disabled (default ""), only the hybrid tag is queried
    and every discovered file is hybrid — one OCS query, as before the feature."""
    nc = _client_for({"vector-index": [{"id": "1", "path": "/a.pdf", "etag": "e1"}]})
    files = await _discover_tagged_files(nc, _settings(keyword_tag=""))

    assert [f["_index_mode"] for f in files] == [payload_keys.INDEX_MODE_HYBRID]
    nc.find_files_by_tag.assert_awaited_once_with(
        "vector-index", mime_type_filter="application/pdf"
    )


@pytest.mark.unit
async def test_both_tags_stamp_modes_with_hybrid_precedence():
    """A file tagged both wins hybrid (appears once); keyword-only files are
    keyword; hybrid-only files are hybrid."""
    nc = _client_for(
        {
            "vector-index": [
                {"id": "1", "path": "/hybrid.pdf", "etag": "e1"},
                {"id": "2", "path": "/both.pdf", "etag": "e2"},
            ],
            "keyword-index": [
                {"id": "2", "path": "/both.pdf", "etag": "e2"},  # also hybrid
                {"id": "3", "path": "/keyword.pdf", "etag": "e3"},
            ],
        }
    )
    files = await _discover_tagged_files(nc, _settings(keyword_tag="keyword-index"))

    by_id = {f["id"]: f["_index_mode"] for f in files}
    assert by_id == {
        "1": payload_keys.INDEX_MODE_HYBRID,
        "2": payload_keys.INDEX_MODE_HYBRID,  # hybrid wins over keyword
        "3": payload_keys.INDEX_MODE_KEYWORD,
    }
    # File 2 appears exactly once (deduped by hybrid precedence).
    assert [f["id"] for f in files].count("2") == 1


@pytest.mark.unit
async def test_reconcile_sets_keyword_mode(monkeypatch):
    """A keyword-index-tagged fileid reconciles to an index task with keyword mode."""
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.processor.get_settings",
        lambda: _settings(keyword_tag="keyword-index"),
    )
    nc = _client_for(
        {
            "vector-index": [{"id": "10", "path": "/h.pdf", "etag": "eh"}],
            "keyword-index": [{"id": "20", "path": "/k.pdf", "etag": "ek"}],
        }
    )
    task = DocumentTask(
        user_id="alice",
        doc_id="20",
        doc_type="file",
        operation="index",
        modified_at=0,
        file_path=None,
    )
    await _reconcile_tag_event(task, nc)

    assert task.operation == "index"
    assert task.index_mode == payload_keys.INDEX_MODE_KEYWORD
    assert task.file_path == "/k.pdf"
