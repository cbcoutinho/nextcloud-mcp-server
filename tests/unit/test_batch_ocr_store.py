"""Unit tests for the batch OCR job-tracking store (Deck #332).

Runs against a real temp-SQLite ``RefreshTokenStorage`` (its ``initialize()``
applies the migrations, incl. ``batch_ocr_jobs``).
"""

import tempfile
from pathlib import Path

import pytest

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.vector.batch_ocr_store import BatchOcrJobStore

pytestmark = pytest.mark.unit


@pytest.fixture
async def store():
    with tempfile.TemporaryDirectory() as tmp:
        storage = RefreshTokenStorage(db_path=str(Path(tmp) / "batch.db"))
        await storage.initialize()
        yield BatchOcrJobStore(storage)


_DOC = dict(user_id="u1", doc_id="d1", doc_type="file", etag="v1")


async def test_get_missing_returns_none(store):
    assert await store.get(**_DOC) is None


async def test_insert_then_get(store):
    await store.insert_pending(**_DOC, job_id="mistral/j1")
    job = await store.get(**_DOC)
    assert job is not None
    assert job.job_id == "mistral/j1"
    assert job.submitted_at > 0


async def test_insert_is_idempotent_on_conflict(store):
    await store.insert_pending(**_DOC, job_id="mistral/j1", submitted_at=100)
    # A racing re-submit must not overwrite the first row's job id.
    await store.insert_pending(**_DOC, job_id="mistral/j2", submitted_at=200)
    job = await store.get(**_DOC)
    assert job.job_id == "mistral/j1"
    assert job.submitted_at == 100


async def test_delete(store):
    await store.insert_pending(**_DOC, job_id="mistral/j1")
    await store.delete(**_DOC)
    assert await store.get(**_DOC) is None


async def test_delete_stale_for_doc_keeps_current_etag(store):
    await store.insert_pending(
        user_id="u1", doc_id="d1", doc_type="file", etag="old", job_id="mistral/old"
    )
    await store.insert_pending(
        user_id="u1", doc_id="d1", doc_type="file", etag="new", job_id="mistral/new"
    )
    await store.delete_stale_for_doc(
        user_id="u1", doc_id="d1", doc_type="file", keep_etag="new"
    )
    # Old version row gone; current one kept.
    assert (
        await store.get(user_id="u1", doc_id="d1", doc_type="file", etag="old")
    ) is None
    kept = await store.get(user_id="u1", doc_id="d1", doc_type="file", etag="new")
    assert kept is not None and kept.job_id == "mistral/new"


async def test_rows_are_scoped_per_document(store):
    await store.insert_pending(**_DOC, job_id="mistral/j1")
    other = await store.get(user_id="u1", doc_id="d2", doc_type="file", etag="v1")
    assert other is None  # different doc_id
