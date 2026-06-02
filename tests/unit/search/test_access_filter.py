"""Tests for nextcloud_mcp_server.search.access_filter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import FieldCondition, MatchText, Range

from nextcloud_mcp_server.search import access_filter
from nextcloud_mcp_server.search.access_filter import (
    build_base_filter_conditions,
    build_ownership_filter,
    clear_accessible_owners_cache,
    list_accessible_owners,
)


@pytest.fixture(autouse=True)
def _reset_owners_cache():
    """The accessible-owners cache is process-global; reset it around each test
    so the shared "alice" user_id can't leak cached results between tests."""
    clear_accessible_owners_cache()
    yield
    clear_accessible_owners_cache()


class TestListAccessibleOwners:
    @pytest.mark.unit
    async def test_includes_self_even_with_no_shares(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.return_value = []

        owners = await list_accessible_owners(sharing, "alice")
        assert owners == ["alice"]

    @pytest.mark.unit
    async def test_collects_uid_owner_from_shares(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.return_value = [
            {"uid_owner": "bob", "share_with": "alice"},
            {"uid_owner": "carol", "share_with": "alice"},
        ]

        owners = await list_accessible_owners(sharing, "alice")
        assert set(owners) == {"alice", "bob", "carol"}

    @pytest.mark.unit
    async def test_deduplicates_repeated_owners(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.return_value = [
            {"uid_owner": "bob"},
            {"uid_owner": "bob"},  # same owner shares many files
            {"uid_owner": "bob"},
        ]

        owners = await list_accessible_owners(sharing, "alice")
        assert sorted(owners) == ["alice", "bob"]

    @pytest.mark.unit
    async def test_falls_back_to_owner_field_when_uid_owner_missing(self) -> None:
        # Some Nextcloud versions surface `owner` instead of `uid_owner`
        # on the shared-with-me response.
        sharing = AsyncMock()
        sharing.list_shares.return_value = [{"owner": "bob"}]

        owners = await list_accessible_owners(sharing, "alice")
        assert sorted(owners) == ["alice", "bob"]

    @pytest.mark.unit
    async def test_ignores_share_with_no_owner_field(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.return_value = [
            {"id": 42},  # malformed share entry
            {"uid_owner": "bob"},
            {"uid_owner": 12345},  # non-string owner — skip
        ]

        owners = await list_accessible_owners(sharing, "alice")
        assert sorted(owners) == ["alice", "bob"]

    @pytest.mark.unit
    async def test_degrades_to_self_on_sharing_api_failure(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.side_effect = RuntimeError("OCS down")

        owners = await list_accessible_owners(sharing, "alice")
        # Fail-open to "self only" rather than blowing up search.
        assert owners == ["alice"]

    @pytest.mark.unit
    async def test_calls_shared_with_me(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.return_value = []

        await list_accessible_owners(sharing, "alice")
        sharing.list_shares.assert_awaited_once_with(shared_with_me=True)


class TestOwnersCacheBehavior:
    @pytest.mark.unit
    async def test_second_call_within_ttl_uses_cache(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.return_value = [{"uid_owner": "bob"}]

        first = await list_accessible_owners(sharing, "alice")
        second = await list_accessible_owners(sharing, "alice")

        assert sorted(first) == ["alice", "bob"]
        assert second == first
        # Only one OCS round-trip — the second call was served from cache.
        sharing.list_shares.assert_awaited_once()

    @pytest.mark.unit
    async def test_expired_entry_triggers_fresh_ocs_call(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.return_value = [{"uid_owner": "bob"}]

        await list_accessible_owners(sharing, "alice")
        # Age the cached entry past the TTL without sleeping/patching the clock.
        ts, value = access_filter._owners_cache["alice"]
        access_filter._owners_cache["alice"] = (
            ts - access_filter._OWNERS_CACHE_TTL_SECONDS - 1.0,
            value,
        )
        await list_accessible_owners(sharing, "alice")

        assert sharing.list_shares.await_count == 2

    @pytest.mark.unit
    async def test_failure_is_not_cached(self) -> None:
        sharing = AsyncMock()
        sharing.list_shares.side_effect = RuntimeError("OCS down")

        await list_accessible_owners(sharing, "alice")  # degrades to self-only
        # A later success must not be masked by a cached failure.
        sharing.list_shares.side_effect = None
        sharing.list_shares.return_value = [{"uid_owner": "bob"}]

        owners = await list_accessible_owners(sharing, "alice")
        assert sorted(owners) == ["alice", "bob"]

    @pytest.mark.unit
    async def test_cache_is_bounded_lru(self, monkeypatch) -> None:
        monkeypatch.setattr(access_filter, "_OWNERS_CACHE_MAXSIZE", 2)
        sharing = AsyncMock()
        sharing.list_shares.return_value = []

        await list_accessible_owners(sharing, "u1")
        await list_accessible_owners(sharing, "u2")
        await list_accessible_owners(sharing, "u3")  # evicts u1 (least recent)

        assert set(access_filter._owners_cache.keys()) == {"u2", "u3"}
        assert len(access_filter._owners_cache) == 2


class TestBuildOwnershipFilter:
    def test_defaults_to_self_only_when_owners_omitted(self) -> None:
        flt = build_ownership_filter("alice")

        # Self-only: just the user_id branch. Self is NOT duplicated into an
        # owner_id branch (the user_id branch already covers self-owned content).
        assert flt.should is not None
        assert len(flt.should) == 1
        (user_branch,) = flt.should
        assert user_branch.key == "user_id"
        assert user_branch.match.value == "alice"

    def test_expands_owner_branch_with_accessible_owners(self) -> None:
        flt = build_ownership_filter("alice", ["alice", "bob", "carol"])

        owner_branch, user_branch = flt.should
        # Owner branch holds only the OTHER owners — self ("alice") is excluded
        # because the user_id branch already matches self-owned content.
        assert set(owner_branch.match.any) == {"bob", "carol"}
        assert user_branch.key == "user_id"
        assert user_branch.match.value == "alice"

    def test_explicit_empty_list_omits_owner_branch_keeps_legacy(self) -> None:
        # Edge case: caller passed an explicit empty list. The owner_id branch
        # is omitted entirely (rather than relying on MatchAny(any=[]) matching
        # nothing); the legacy user_id branch remains as the safety net so the
        # user still finds their own content from before the migration.
        flt = build_ownership_filter("alice", [])

        assert flt.should is not None
        assert len(flt.should) == 1
        (user_branch,) = flt.should
        assert user_branch.key == "user_id"
        assert user_branch.match.value == "alice"


class TestBuildBaseFilterConditions:
    """The shared ADR-027 filter contract used by both search algorithms."""

    @pytest.mark.unit
    def test_minimal_is_placeholder_plus_ownership(self) -> None:
        # No doc_type, no date bounds -> exactly placeholder + ownership.
        conditions = build_base_filter_conditions("alice", None)
        assert len(conditions) == 2
        # No modified_at Range condition present.
        assert not any(
            isinstance(c, FieldCondition) and c.key == "modified_at" for c in conditions
        )

    @pytest.mark.unit
    def test_doc_type_appends_match_condition(self) -> None:
        conditions = build_base_filter_conditions("alice", None, doc_type="note")
        doc_type_conds = [
            c
            for c in conditions
            if isinstance(c, FieldCondition) and c.key == "doc_type"
        ]
        assert len(doc_type_conds) == 1
        assert doc_type_conds[0].match.value == "note"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "after,before,expected_gte,expected_lte",
        [
            (100, 200, 100, 200),
            (100, None, 100, None),  # after-only
            (None, 200, None, 200),  # before-only
        ],
    )
    def test_modified_at_range_appended(
        self, after, before, expected_gte, expected_lte
    ) -> None:
        conditions = build_base_filter_conditions(
            "alice", None, modified_after=after, modified_before=before
        )
        range_conds = [
            c
            for c in conditions
            if isinstance(c, FieldCondition) and c.key == "modified_at"
        ]
        assert len(range_conds) == 1
        rng = range_conds[0].range
        assert isinstance(rng, Range)
        assert rng.gte == expected_gte
        assert rng.lte == expected_lte

    @pytest.mark.unit
    def test_no_range_when_both_bounds_none(self) -> None:
        conditions = build_base_filter_conditions(
            "alice", None, modified_after=None, modified_before=None
        )
        assert not any(
            isinstance(c, FieldCondition) and c.key == "modified_at" for c in conditions
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("prefix", ["/Projects/Reports", "/Archive"])
    def test_path_prefix_appends_file_path_match_text(self, prefix) -> None:
        conditions = build_base_filter_conditions("alice", None, path_prefix=prefix)
        path_conds = [
            c
            for c in conditions
            if isinstance(c, FieldCondition) and c.key == "file_path"
        ]
        assert len(path_conds) == 1
        match = path_conds[0].match
        assert isinstance(match, MatchText)
        assert match.text == prefix

    @pytest.mark.unit
    def test_no_path_condition_when_prefix_absent(self) -> None:
        conditions = build_base_filter_conditions("alice", None, path_prefix=None)
        assert not any(
            isinstance(c, FieldCondition) and c.key == "file_path" for c in conditions
        )

    @pytest.mark.unit
    def test_all_filters_compose(self) -> None:
        # placeholder + ownership + doc_type + modified_at range + file_path = 5.
        conditions = build_base_filter_conditions(
            "alice",
            ["alice", "bob"],
            doc_type="file",
            modified_after=100,
            modified_before=200,
            path_prefix="/Projects",
        )
        assert len(conditions) == 5
