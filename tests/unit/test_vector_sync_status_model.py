"""Unit tests for VectorSyncStatusResponse documents-vs-chunks fields."""

import pytest

from nextcloud_mcp_server.models.semantic import VectorSyncStatusResponse

pytestmark = pytest.mark.unit


def test_vector_sync_status_documents_and_chunks() -> None:
    """Exposes documents AND chunks; indexed_count is a deprecated chunks alias."""
    response = VectorSyncStatusResponse(
        indexed_documents=486,
        indexed_chunks=16039,
        indexed_count=16039,  # deprecated alias
        pending_count=2214,
        status="syncing",
        enabled=True,
        ingest_queue="memory",
    )

    data = response.model_dump()
    assert data["indexed_documents"] == 486
    assert data["indexed_chunks"] == 16039
    # Alias mirrors chunks (not documents) for back-compat.
    assert data["indexed_count"] == data["indexed_chunks"]
    assert data["pending_count"] == 2214


def test_vector_sync_status_defaults_zeroed() -> None:
    """New corpus fields default to 0 (disabled / pre-sync path)."""
    response = VectorSyncStatusResponse(status="disabled", enabled=False)
    data = response.model_dump()
    assert data["indexed_documents"] == 0
    assert data["indexed_chunks"] == 0
    assert data["indexed_count"] == 0
