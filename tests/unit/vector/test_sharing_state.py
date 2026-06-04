"""Unit tests for tenant-wide content dedup + observed-access ACL state.

Covers vector/sharing_state.py: the tenant-wide content lookup that lets a
shared/group-folder file be parsed+embedded once per tenant instead of once per
user, and the ``acl_principals`` maintenance (grant/release) that keeps a
deduplicated point findable by every reader without re-indexing.

All functions reach Qdrant via ``get_qdrant_client`` and resolve the collection
via ``get_settings``; both are monkeypatched here so the logic is exercised
without a live Qdrant.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector import sharing_state as ss

pytestmark = pytest.mark.unit

_COLLECTION = "test_collection"
_MODEL = "model-x"


class _Settings:
    def get_collection_name(self) -> str:
        return _COLLECTION

    def get_embedding_model_name(self) -> str:
        return _MODEL


def _point(payload: dict) -> SimpleNamespace:
    """Stand-in for a qdrant_client Record (only id/payload are read)."""
    return SimpleNamespace(id="pt", payload=payload)


@pytest.fixture
def client(monkeypatch) -> AsyncMock:
    """An AsyncMock Qdrant client wired into sharing_state, with a stub Settings.

    ``scroll`` defaults to "no points"; individual tests override
    ``client.scroll.return_value``/``side_effect``.
    """
    qc = AsyncMock()
    qc.scroll.return_value = ([], None)
    monkeypatch.setattr(ss, "get_qdrant_client", AsyncMock(return_value=qc))
    monkeypatch.setattr(ss, "get_settings", lambda: _Settings())
    return qc


def _must_keys(flt) -> list[str | None]:
    """Collect the FieldCondition keys in a Filter's ``must`` clause."""
    return [getattr(c, "key", None) for c in (flt.must or [])]


class TestFindIndexedContent:
    async def test_returns_payload_on_etag_and_model_match(self, client) -> None:
        payload = {
            "doc_id": "42",
            "etag": "abc",
            payload_keys.EMBEDDING_IDENTITY: _MODEL,
            ss.ACL_PRINCIPALS_KEY: ["user:alice"],
        }
        client.scroll.return_value = ([_point(payload)], None)

        result = await ss.find_indexed_content("42", "file", "abc", _MODEL)
        assert result == payload

    async def test_none_when_no_points(self, client) -> None:
        client.scroll.return_value = ([], None)
        assert await ss.find_indexed_content("42", "file", "abc", _MODEL) is None

    async def test_none_on_embedding_model_mismatch(self, client) -> None:
        # A model switch overwrites the same point IDs; existing vectors made by
        # a different model must be re-embedded, so this reports "not indexed".
        client.scroll.return_value = (
            [_point({payload_keys.EMBEDDING_IDENTITY: "other-model"})],
            None,
        )
        assert await ss.find_indexed_content("42", "file", "abc", _MODEL) is None

    async def test_empty_etag_short_circuits_without_query(self, client) -> None:
        assert await ss.find_indexed_content("42", "file", "", _MODEL) is None
        client.scroll.assert_not_called()


class TestAddPrincipal:
    async def test_noop_when_principal_already_present(self, client) -> None:
        added = await ss.add_principal("42", "file", "alice", ["user:alice"])
        assert added is False
        client.set_payload.assert_not_called()

    async def test_unions_principal_when_absent(self, client) -> None:
        added = await ss.add_principal("42", "file", "bob", ["user:alice"])
        assert added is True
        client.set_payload.assert_awaited_once()
        kwargs = client.set_payload.await_args.kwargs
        assert kwargs["payload"][ss.ACL_PRINCIPALS_KEY] == ["user:alice", "user:bob"]
        # Updates only real (non-placeholder) chunks of this document.
        assert _must_keys(kwargs["points"]) == ["doc_id", "doc_type", "is_placeholder"]

    async def test_handles_none_current_principals(self, client) -> None:
        added = await ss.add_principal("42", "file", "alice", None)
        assert added is True
        kwargs = client.set_payload.await_args.kwargs
        assert kwargs["payload"][ss.ACL_PRINCIPALS_KEY] == ["user:alice"]


class TestClaimExistingIndex:
    async def test_true_and_grants_principal_on_hit(self, client) -> None:
        client.scroll.return_value = (
            [
                _point(
                    {
                        payload_keys.EMBEDDING_IDENTITY: _MODEL,
                        ss.ACL_PRINCIPALS_KEY: ["user:alice"],
                    }
                )
            ],
            None,
        )
        claimed = await ss.claim_existing_index("42", "file", "abc", "bob")
        assert claimed is True
        # bob was added to the existing point's principals.
        client.set_payload.assert_awaited_once()
        assert client.set_payload.await_args.kwargs["payload"][
            ss.ACL_PRINCIPALS_KEY
        ] == ["user:alice", "user:bob"]

    async def test_false_when_not_indexed(self, client) -> None:
        client.scroll.return_value = ([], None)
        assert await ss.claim_existing_index("42", "file", "abc", "bob") is False
        client.set_payload.assert_not_called()

    async def test_hit_for_already_listed_user_writes_nothing(self, client) -> None:
        client.scroll.return_value = (
            [
                _point(
                    {
                        payload_keys.EMBEDDING_IDENTITY: _MODEL,
                        ss.ACL_PRINCIPALS_KEY: ["user:alice"],
                    }
                )
            ],
            None,
        )
        # alice already present -> claim still True (skip reprocess) but no write.
        assert await ss.claim_existing_index("42", "file", "abc", "alice") is True
        client.set_payload.assert_not_called()

    async def test_lookup_error_degrades_to_process_normally(self, client) -> None:
        # A Qdrant hiccup during dedup must not abort the scan — fall back to
        # processing the document (return False), not raise.
        client.scroll.side_effect = RuntimeError("qdrant down")
        assert await ss.claim_existing_index("42", "file", "abc", "bob") is False

    async def test_principal_grant_failure_after_hit_is_non_fatal(self, client) -> None:
        # The content IS indexed (skip reprocess), so a failure to record the
        # principal still returns True; verify-on-read + next scan reconcile.
        client.scroll.return_value = (
            [_point({payload_keys.EMBEDDING_IDENTITY: _MODEL})],
            None,
        )
        client.set_payload.side_effect = RuntimeError("set_payload failed")
        assert await ss.claim_existing_index("42", "file", "abc", "bob") is True


class TestExistingPrincipals:
    async def test_returns_recorded_principals(self, client) -> None:
        client.scroll.return_value = (
            [_point({ss.ACL_PRINCIPALS_KEY: ["user:alice", "user:bob"]})],
            None,
        )
        assert await ss.existing_principals("42", "file") == ["user:alice", "user:bob"]

    async def test_empty_when_no_points(self, client) -> None:
        client.scroll.return_value = ([], None)
        assert await ss.existing_principals("42", "file") == []


class TestReleaseDocumentForUser:
    async def test_keeps_points_and_trims_principals_when_readers_remain(
        self, client
    ) -> None:
        client.scroll.return_value = (
            [_point({ss.ACL_PRINCIPALS_KEY: ["user:alice", "user:bob"]})],
            None,
        )
        await ss.release_document_for_user("42", "file", "alice")

        client.delete.assert_not_called()
        kwargs = client.set_payload.await_args.kwargs
        assert kwargs["payload"][ss.ACL_PRINCIPALS_KEY] == ["user:bob"]

    async def test_deletes_all_points_when_last_reader_released(self, client) -> None:
        client.scroll.return_value = (
            [_point({ss.ACL_PRINCIPALS_KEY: ["user:alice"]})],
            None,
        )
        await ss.release_document_for_user("42", "file", "alice")

        client.set_payload.assert_not_called()
        client.delete.assert_awaited_once()
        selector = client.delete.await_args.kwargs["points_selector"]
        # Whole document removed: doc_id + doc_type, no user_id, no placeholder gate.
        assert _must_keys(selector) == ["doc_id", "doc_type"]

    async def test_legacy_points_without_principals_delete_by_user(
        self, client
    ) -> None:
        # Pre-acl_principals points: preserve the original per-user delete.
        client.scroll.return_value = ([_point({"doc_id": "42"})], None)
        await ss.release_document_for_user("42", "file", "alice")

        client.set_payload.assert_not_called()
        selector = client.delete.await_args.kwargs["points_selector"]
        assert _must_keys(selector) == ["user_id", "doc_id", "doc_type"]

    async def test_no_points_falls_back_to_per_user_delete(self, client) -> None:
        client.scroll.return_value = ([], None)
        await ss.release_document_for_user("42", "file", "alice")

        selector = client.delete.await_args.kwargs["points_selector"]
        assert _must_keys(selector) == ["user_id", "doc_id", "doc_type"]
