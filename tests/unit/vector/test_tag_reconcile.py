"""Unit tests for processor._reconcile_tag_event.

A SystemTag MapperEvent enqueues a file task with only a fileid (file_path is
None). The reconcile resolves the file's *current* ``vector-index`` membership
and mutates the task into a concrete index (path/etag filled) or a delete.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.vector.processor import _reconcile_tag_event
from nextcloud_mcp_server.vector.scanner import DocumentTask


def _tag_task(doc_id: str = "478087") -> DocumentTask:
    return DocumentTask(
        user_id="alice",
        doc_id=doc_id,
        doc_type="file",
        operation="index",
        modified_at=0,
        file_path=None,
    )


@pytest.mark.unit
async def test_reconcile_tagged_file_becomes_index():
    """A fileid still carrying the tag is resolved to a concrete index task."""
    nc_client = MagicMock()
    nc_client.find_files_by_tag = AsyncMock(
        return_value=[
            {
                "id": "478087",
                "path": "/alice/files/Docs/report.pdf",
                "etag": "abc123",
                "last_modified_timestamp": 1762850245,
            }
        ]
    )

    task = _tag_task("478087")
    await _reconcile_tag_event(task, nc_client)

    assert task.operation == "index"
    assert task.file_path == "/alice/files/Docs/report.pdf"
    assert task.etag == "abc123"
    assert task.modified_at == 1762850245
    nc_client.find_files_by_tag.assert_awaited_once_with(
        "vector-index", mime_type_filter="application/pdf"
    )


@pytest.mark.unit
async def test_reconcile_untagged_file_becomes_delete():
    """A fileid absent from the tagged set flips the task to a delete."""
    nc_client = MagicMock()
    nc_client.find_files_by_tag = AsyncMock(
        return_value=[
            {"id": "999", "path": "/alice/files/other.pdf", "etag": "z"},
        ]
    )

    task = _tag_task("478087")
    await _reconcile_tag_event(task, nc_client)

    assert task.operation == "delete"
    # Path stays None — the delete path addresses points by doc_id only.
    assert task.file_path is None


@pytest.mark.unit
async def test_reconcile_preserves_existing_etag():
    """An etag already on the task is not overwritten by the tag listing."""
    nc_client = MagicMock()
    nc_client.find_files_by_tag = AsyncMock(
        return_value=[
            {"id": "478087", "path": "/alice/files/r.pdf", "etag": "from-listing"}
        ]
    )

    task = _tag_task("478087")
    task.etag = "preset"
    await _reconcile_tag_event(task, nc_client)

    assert task.etag == "preset"
    assert task.file_path == "/alice/files/r.pdf"
