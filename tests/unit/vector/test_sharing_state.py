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
    # The dedup identity is always the dense embedding model name (keyword-vs-
    # hybrid is tracked per-document via payload_keys.INDEX_MODE, not the identity).
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


class TestFileTitleFromPath:
    def test_uses_basename(self) -> None:
        assert ss.file_title_from_path("/Documents/report.pdf") == "report.pdf"

    def test_no_directory(self) -> None:
        assert ss.file_title_from_path("report.pdf") == "report.pdf"

    def test_trailing_slash_ignored(self) -> None:
        assert ss.file_title_from_path("/a/b/c/") == "c"

    def test_root_only_falls_back_to_input(self) -> None:
        # Degenerate path with no basename — return the input rather than "".
        assert ss.file_title_from_path("/") == "/"


class TestReconcileDocumentPath:
    async def test_noop_when_path_unchanged(self, client) -> None:
        changed = await ss.reconcile_document_path(
            "42", "file", "/a/old.pdf", "/a/old.pdf"
        )
        assert changed is False
        client.set_payload.assert_not_called()

    async def test_noop_when_current_path_empty(self, client) -> None:
        changed = await ss.reconcile_document_path("42", "file", "/a/old.pdf", "")
        assert changed is False
        client.set_payload.assert_not_called()

    async def test_rewrites_path_and_title_on_rename(self, client) -> None:
        changed = await ss.reconcile_document_path(
            "42", "file", "/a/old.pdf", "/a/new-name.pdf"
        )
        assert changed is True
        client.set_payload.assert_awaited_once()
        kwargs = client.set_payload.await_args.kwargs
        assert kwargs["payload"]["file_path"] == "/a/new-name.pdf"
        assert kwargs["payload"]["title"] == "new-name.pdf"
        # Only real (non-placeholder) chunks of this document are updated.
        assert _must_keys(kwargs["points"]) == ["doc_id", "doc_type", "is_placeholder"]

    async def test_backfills_when_no_stored_path(self, client) -> None:
        # Legacy point with no stored file_path -> treated as changed (backfill).
        changed = await ss.reconcile_document_path("42", "file", None, "/a/new.pdf")
        assert changed is True
        kwargs = client.set_payload.await_args.kwargs
        assert kwargs["payload"]["title"] == "new.pdf"

    async def test_suppresses_non_owner_divergent_path(self, client) -> None:
        # ADR-033 Phase 1: a reader (bob) observing the shared file at their own
        # mount path must NOT overwrite the owner-pinned scalar. This is the
        # thrash guard — no set_payload is issued.
        changed = await ss.reconcile_document_path(
            "42",
            "file",
            "/alice/doc.pdf",
            "/bob/alice/doc.pdf",
            caller_user_id="bob",
            owner_id="alice",
        )
        assert changed is False
        client.set_payload.assert_not_called()

    async def test_rewrites_for_owner_rename(self, client) -> None:
        # The owner renaming their own file is a genuine change -> reconcile.
        changed = await ss.reconcile_document_path(
            "42",
            "file",
            "/alice/old.pdf",
            "/alice/new.pdf",
            caller_user_id="alice",
            owner_id="alice",
        )
        assert changed is True
        kwargs = client.set_payload.await_args.kwargs
        assert kwargs["payload"]["file_path"] == "/alice/new.pdf"

    async def test_legacy_point_without_owner_still_reconciles(self, client) -> None:
        # owner_id unknown (pre-owner_id point) -> fall through to the old
        # always-reconcile behaviour (single-owner, so no thrash to guard).
        changed = await ss.reconcile_document_path(
            "42",
            "file",
            "/a/old.pdf",
            "/a/new.pdf",
            caller_user_id="bob",
            owner_id=None,
        )
        assert changed is True
        client.set_payload.assert_awaited_once()

    async def test_backfill_empty_stored_path_regardless_of_caller(
        self, client
    ) -> None:
        # An empty stored path has nothing to thrash: a non-owner may backfill it
        # (the owner's next scan re-pins it if it diverges).
        changed = await ss.reconcile_document_path(
            "42",
            "file",
            "",
            "/bob/alice/doc.pdf",
            caller_user_id="bob",
            owner_id="alice",
        )
        assert changed is True
        client.set_payload.assert_awaited_once()


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

    async def test_dedup_hit_reconciles_stale_path(self, client) -> None:
        # Same content (etag) at a new path = a rename the dedup would otherwise
        # skip. The user is already a principal, so the only write is the path
        # reconcile (one set_payload with the refreshed file_path + title).
        client.scroll.return_value = (
            [
                _point(
                    {
                        payload_keys.EMBEDDING_IDENTITY: _MODEL,
                        ss.ACL_PRINCIPALS_KEY: ["user:bob"],
                        "file_path": "/a/old.pdf",
                    }
                )
            ],
            None,
        )
        claimed = await ss.claim_existing_index(
            "42", "file", "abc", "bob", current_path="/a/new.pdf"
        )
        assert claimed is True
        client.set_payload.assert_awaited_once()
        payload = client.set_payload.await_args.kwargs["payload"]
        assert payload["file_path"] == "/a/new.pdf"
        assert payload["title"] == "new.pdf"

    async def test_dedup_hit_reconciles_and_grants_new_principal(self, client) -> None:
        # Renamed file (stale path) AND a user not yet in the ACL: both writes
        # fire — one set_payload for file_path/title, one for acl_principals.
        client.scroll.return_value = (
            [
                _point(
                    {
                        payload_keys.EMBEDDING_IDENTITY: _MODEL,
                        ss.ACL_PRINCIPALS_KEY: ["user:alice"],
                        "file_path": "/a/old.pdf",
                    }
                )
            ],
            None,
        )
        claimed = await ss.claim_existing_index(
            "42", "file", "abc", "bob", current_path="/a/new.pdf"
        )
        assert claimed is True
        assert client.set_payload.await_count == 2
        payloads = [c.kwargs["payload"] for c in client.set_payload.await_args_list]
        # One write refreshes the path/title, the other unions the new principal.
        assert {"file_path": "/a/new.pdf", "title": "new.pdf"} in payloads
        assert {ss.ACL_PRINCIPALS_KEY: ["user:alice", "user:bob"]} in payloads

    async def test_dedup_hit_without_current_path_skips_reconcile(self, client) -> None:
        # No current_path (non-file callers) -> never touches file_path/title.
        client.scroll.return_value = (
            [
                _point(
                    {
                        payload_keys.EMBEDDING_IDENTITY: _MODEL,
                        ss.ACL_PRINCIPALS_KEY: ["user:bob"],
                        "file_path": "/a/old.pdf",
                    }
                )
            ],
            None,
        )
        assert await ss.claim_existing_index("42", "file", "abc", "bob") is True
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

    async def test_hybrid_claim_misses_existing_keyword_point(self, client) -> None:
        # Monotonic keyword→hybrid upgrade: a hybrid claim cannot reuse a
        # sparse-only keyword point (it lacks the dense vector), so — even though
        # the embedding identity matches (same model) — it MUST miss and force a
        # reprocess that adds the dense vector. No principal is granted on a miss.
        client.scroll.return_value = (
            [
                _point(
                    {
                        payload_keys.EMBEDDING_IDENTITY: _MODEL,
                        payload_keys.INDEX_MODE: payload_keys.INDEX_MODE_KEYWORD,
                        ss.ACL_PRINCIPALS_KEY: ["user:alice"],
                    }
                )
            ],
            None,
        )
        assert (
            await ss.find_indexed_content(
                "42",
                "file",
                "abc",
                _MODEL,
                index_mode=payload_keys.INDEX_MODE_HYBRID,
            )
            is None
        )
        # claim_existing_index defaults to index_mode="hybrid" → same miss → False.
        assert await ss.claim_existing_index("42", "file", "abc", "bob") is False
        client.set_payload.assert_not_called()

    async def test_keyword_claim_hits_existing_hybrid_point(self, client) -> None:
        # hybrid ⊇ keyword: a keyword claim reuses an existing hybrid point rather
        # than downgrading/stripping the dense vector while a hybrid reader holds
        # the tag. alice already listed → HIT (skip reprocess), no write.
        client.scroll.return_value = (
            [
                _point(
                    {
                        payload_keys.EMBEDDING_IDENTITY: _MODEL,
                        payload_keys.INDEX_MODE: payload_keys.INDEX_MODE_HYBRID,
                        ss.ACL_PRINCIPALS_KEY: ["user:alice"],
                    }
                )
            ],
            None,
        )
        assert (
            await ss.claim_existing_index(
                "42",
                "file",
                "abc",
                "alice",
                index_mode=payload_keys.INDEX_MODE_KEYWORD,
            )
            is True
        )
        client.set_payload.assert_not_called()

    async def test_same_mode_claim_hits(self, client) -> None:
        # Same-mode (keyword claim vs existing keyword point) is a hit as before —
        # the monotonic rule only forces a miss for hybrid-over-keyword.
        client.scroll.return_value = (
            [
                _point(
                    {
                        payload_keys.EMBEDDING_IDENTITY: _MODEL,
                        payload_keys.INDEX_MODE: payload_keys.INDEX_MODE_KEYWORD,
                        ss.ACL_PRINCIPALS_KEY: ["user:alice"],
                    }
                )
            ],
            None,
        )
        assert (
            await ss.claim_existing_index(
                "42",
                "file",
                "abc",
                "alice",
                index_mode=payload_keys.INDEX_MODE_KEYWORD,
            )
            is True
        )
        client.set_payload.assert_not_called()

    async def test_missing_index_mode_defaults_to_hybrid(self, client) -> None:
        # A legacy point written before INDEX_MODE existed has no key; it defaults
        # to "hybrid", so a hybrid claim still dedups against it (HIT).
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
        assert (
            await ss.find_indexed_content(
                "42",
                "file",
                "abc",
                _MODEL,
                index_mode=payload_keys.INDEX_MODE_HYBRID,
            )
            is not None
        )

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


class _StubWebdav:
    """Resolves ancestor folder paths to fileids for the backfill tests."""

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping

    async def get_fileid(self, path: str):
        return self._mapping.get(path)


def _folder_ancestor_payloads(client) -> list[list]:
    """The folder_ancestors values from every set_payload call, in order."""
    return [
        call.kwargs["payload"][payload_keys.FOLDER_ANCESTORS]
        for call in client.set_payload.await_args_list
        if payload_keys.FOLDER_ANCESTORS in call.kwargs["payload"]
    ]


class TestClaimFolderAncestorBackfill:
    def _hit(self, **extra) -> dict:
        payload = {
            payload_keys.EMBEDDING_IDENTITY: _MODEL,
            ss.ACL_PRINCIPALS_KEY: ["user:alice"],
            "file_path": "/A/doc.pdf",
            "owner_id": "alice",
        }
        payload.update(extra)
        return payload

    async def test_backfills_folder_ancestors_for_owner(self, client) -> None:
        # Owner (alice) re-scans a pre-Phase-3 doc (no folder_ancestors); the
        # ancestors are resolved from her canonical path and written once.
        client.scroll.return_value = ([_point(self._hit())], None)
        webdav = _StubWebdav({"/A": "100"})

        claimed = await ss.claim_existing_index(
            "42", "file", "abc", "alice", current_path="/A/doc.pdf", webdav=webdav
        )
        assert claimed is True
        assert _folder_ancestor_payloads(client) == [["100"]]

    async def test_skips_backfill_for_non_owner(self, client) -> None:
        # A reader (bob) must NOT backfill — his mount prefix would pollute the
        # canonical, user-agnostic ancestor set.
        client.scroll.return_value = ([_point(self._hit())], None)
        webdav = _StubWebdav({"/bob/A": "999"})

        await ss.claim_existing_index(
            "42", "file", "abc", "bob", current_path="/bob/A/doc.pdf", webdav=webdav
        )
        assert _folder_ancestor_payloads(client) == []

    async def test_skips_backfill_when_already_present(self, client) -> None:
        client.scroll.return_value = (
            [_point(self._hit(**{payload_keys.FOLDER_ANCESTORS: ["777"]}))],
            None,
        )
        webdav = _StubWebdav({"/A": "100"})

        await ss.claim_existing_index(
            "42", "file", "abc", "alice", current_path="/A/doc.pdf", webdav=webdav
        )
        assert _folder_ancestor_payloads(client) == []

    async def test_no_backfill_without_webdav(self, client) -> None:
        client.scroll.return_value = ([_point(self._hit())], None)
        await ss.claim_existing_index(
            "42", "file", "abc", "alice", current_path="/A/doc.pdf"
        )
        assert _folder_ancestor_payloads(client) == []

    async def test_backfills_for_legacy_point_without_owner(self, client) -> None:
        # owner_id unknown (pre-owner_id point) -> single-owner, safe to backfill.
        payload = self._hit()
        del payload["owner_id"]
        client.scroll.return_value = ([_point(payload)], None)
        webdav = _StubWebdav({"/A": "100"})

        await ss.claim_existing_index(
            "42", "file", "abc", "alice", current_path="/A/doc.pdf", webdav=webdav
        )
        assert _folder_ancestor_payloads(client) == [["100"]]


class _CountingWebdav:
    """Counts get_fileid calls to prove the per-scan cache collapses lookups."""

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def get_fileid(self, path: str):
        self.calls.append(path)
        return self._mapping.get(path)


class TestBackfillCacheThreading:
    async def test_shared_cache_resolves_common_ancestor_once(self, client) -> None:
        # Two files under the same folder /A, both needing a Phase-3 backfill.
        # A shared folder_ancestor_cache must resolve /A exactly once.
        payload = {
            payload_keys.EMBEDDING_IDENTITY: _MODEL,
            ss.ACL_PRINCIPALS_KEY: ["user:alice"],
            "owner_id": "alice",
        }
        client.scroll.return_value = ([_point(dict(payload))], None)
        webdav = _CountingWebdav({"/A": "100"})
        cache: dict = {}

        await ss.claim_existing_index(
            "1",
            "file",
            "abc",
            "alice",
            current_path="/A/one.pdf",
            webdav=webdav,
            folder_ancestor_cache=cache,
        )
        await ss.claim_existing_index(
            "2",
            "file",
            "abc",
            "alice",
            current_path="/A/two.pdf",
            webdav=webdav,
            folder_ancestor_cache=cache,
        )
        # /A resolved once across the two files, not twice.
        assert webdav.calls == ["/A"]
        assert cache == {"/A": "100"}
