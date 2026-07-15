"""Unit tests for the fail-safe file-deletion gate (`_plan_file_deletions`).

The scanner deletes indexed Qdrant file points that a scan's tag discovery no
longer returns. A customer-hosted Nextcloud can intermittently answer the
`oc:systemtag` REPORT with a *successful-but-empty* 207, which is
byte-indistinguishable from "the tag genuinely has no files"; naively honoring
that empty read deletes (and then re-ingests) the whole corpus every cycle. The
gate suppresses deletions for an index mode whose discovery came back empty
while Qdrant still holds points for it, until a *consecutive-empty streak*
threshold is reached (a sustained empty = a real mass-untag).

These drive the pure `_plan_file_deletions` helper directly (no Qdrant / stream /
async), passing fresh `grace_state` / `streak_state` dicts per test.
"""

import pytest
from qdrant_client.models import FieldCondition, IsEmptyCondition

from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector import scanner as scanner_module
from nextcloud_mcp_server.vector.scanner import (
    _bump_streak,
    _FileDeletionPlan,
    _indexed_files_scroll_filter,
    _plan_file_deletions,
    _record_suppressed_deletions,
)
from nextcloud_mcp_server.vector.sharing_state import (
    ACL_PRINCIPALS_KEY,
    user_principal,
)

pytestmark = pytest.mark.unit

HYB = payload_keys.INDEX_MODE_HYBRID
KW = payload_keys.INDEX_MODE_KEYWORD

USER = "alice"
GRACE = 450.0
THRESHOLD = 3


def _plan(
    *,
    indexed_by_mode,
    nextcloud_file_ids,
    discovered_by_mode,
    attempted_modes,
    grace_state,
    streak_state,
    now=1000.0,
    grace_period=GRACE,
    threshold=THRESHOLD,
):
    return _plan_file_deletions(
        user_id=USER,
        indexed_by_mode=indexed_by_mode,
        nextcloud_file_ids=nextcloud_file_ids,
        discovered_by_mode=discovered_by_mode,
        attempted_modes=attempted_modes,
        grace_state=grace_state,
        streak_state=streak_state,
        now=now,
        grace_period=grace_period,
        empty_delete_threshold=threshold,
    )


def test_full_empty_discovery_suppresses_all_deletions():
    """Attempted mode returns 0 but Qdrant has points -> suppress, start no grace."""
    grace: dict = {}
    streak: dict = {}
    plan = _plan(
        indexed_by_mode={HYB: {"1", "2", "3"}},
        nextcloud_file_ids=set(),
        discovered_by_mode={},  # 0 discovered
        attempted_modes={HYB},
        grace_state=grace,
        streak_state=streak,
    )
    assert plan.to_delete == []
    assert plan.suppressed_by_mode == {HYB: 3}
    assert plan.streaks == {HYB: 1}
    # Crucially, no grace timers were started for the suppressed points.
    assert grace == {}
    assert streak == {(USER, HYB): 1}


def test_suppression_lifts_after_threshold_then_deletes_past_grace():
    """A *sustained* empty (streak == threshold) resumes deletion via grace."""
    grace: dict = {}
    streak: dict = {}
    common = {
        "indexed_by_mode": {HYB: {"1"}},
        "nextcloud_file_ids": set(),
        "discovered_by_mode": {},
        "attempted_modes": {HYB},
        "grace_state": grace,
        "streak_state": streak,
    }
    # Cycles 1 & 2: below threshold -> suppressed, nothing deleted, no grace.
    p1 = _plan(**common, now=1000.0)
    assert p1.to_delete == [] and p1.suppressed_by_mode == {HYB: 1} and grace == {}
    p2 = _plan(**common, now=1300.0)
    assert p2.to_delete == [] and p2.suppressed_by_mode == {HYB: 1} and grace == {}
    # Cycle 3: streak hits threshold -> no longer suppressed; grace *starts* now.
    p3 = _plan(**common, now=1600.0)
    assert p3.to_delete == []
    assert p3.suppressed_by_mode == {}
    assert grace == {(USER, "1", "file"): 1600.0}
    # Cycle 4: grace elapsed -> the delete finally fires.
    p4 = _plan(**common, now=1600.0 + GRACE)
    assert p4.to_delete == ["1"]
    assert (USER, "1", "file") not in grace


def test_healthy_read_resets_streak_and_resumes_normal_grace():
    """A >0 discovery pops the streak and lets genuinely-missing ids age out."""
    grace: dict = {}
    streak: dict = {(USER, HYB): 2}  # pre-existing streak from earlier flakes
    plan = _plan(
        indexed_by_mode={HYB: {"1", "2"}},
        nextcloud_file_ids={"2"},  # "1" genuinely untagged, "2" still present
        discovered_by_mode={HYB: 1},  # healthy, non-empty
        attempted_modes={HYB},
        grace_state=grace,
        streak_state=streak,
        now=5000.0,
    )
    assert plan.to_delete == []  # first miss -> grace starts, not deleted yet
    assert plan.suppressed_by_mode == {}
    assert streak == {}  # streak key popped by the healthy read
    assert grace == {(USER, "1", "file"): 5000.0}


def test_asymmetric_keyword_empty_suppresses_only_keyword():
    """One mode flaking empty must not block the healthy mode's deletions."""
    grace: dict = {(USER, "h2", "file"): 0.0}  # hybrid miss already past grace
    streak: dict = {}
    plan = _plan(
        indexed_by_mode={HYB: {"h1", "h2"}, KW: {"k1"}},
        nextcloud_file_ids={"h1"},  # h2 genuinely gone; k1 "missing" only via flake
        discovered_by_mode={HYB: 1},  # hybrid healthy; keyword empty (absent key)
        attempted_modes={HYB, KW},
        grace_state=grace,
        streak_state=streak,
        now=10_000.0,
    )
    assert plan.to_delete == ["h2"]  # hybrid deletes normally
    assert plan.suppressed_by_mode == {KW: 1}  # keyword withheld
    assert streak == {(USER, KW): 1}  # only keyword accrues a streak
    assert (USER, "k1", "file") not in grace  # keyword grace untouched


def test_files_disabled_not_suppressed():
    """Admin-disabled files (no mode attempted) -> intentional purge proceeds."""
    grace: dict = {
        (USER, "1", "file"): 0.0,
        (USER, "2", "file"): 0.0,
    }
    streak: dict = {(USER, HYB): 5, (USER, KW): 5}  # stale streaks must clear
    plan = _plan(
        indexed_by_mode={HYB: {"1"}, KW: {"2"}},
        nextcloud_file_ids=set(),
        discovered_by_mode={},
        attempted_modes=set(),  # files disabled -> nothing attempted
        grace_state=grace,
        streak_state=streak,
        now=10_000.0,
    )
    assert sorted(plan.to_delete) == ["1", "2"]
    assert plan.suppressed_by_mode == {}
    assert streak == {}  # unattempted modes reset their streaks


def test_keyword_tag_disabled_purges_legacy_keyword():
    """Keyword tag disabled -> keyword not attempted -> legacy kw points purge."""
    grace: dict = {(USER, "k1", "file"): 0.0}
    streak: dict = {}
    plan = _plan(
        indexed_by_mode={HYB: {"h1"}, KW: {"k1"}},
        nextcloud_file_ids={"h1"},
        discovered_by_mode={HYB: 1},
        attempted_modes={HYB},  # keyword tag disabled -> only hybrid attempted
        grace_state=grace,
        streak_state=streak,
        now=10_000.0,
    )
    assert plan.to_delete == ["k1"]
    assert plan.suppressed_by_mode == {}


def test_normal_untag_deletes_when_discovery_healthy():
    """The legitimate-deletion guarantee: a real untag still deletes."""
    grace: dict = {(USER, "3", "file"): 0.0}  # user untagged file 3 a while ago
    streak: dict = {}
    plan = _plan(
        indexed_by_mode={HYB: {"1", "2", "3"}},
        nextcloud_file_ids={"1", "2"},  # 3 no longer tagged
        discovered_by_mode={HYB: 2},  # healthy, non-empty
        attempted_modes={HYB},
        grace_state=grace,
        streak_state=streak,
        now=10_000.0,
    )
    assert plan.to_delete == ["3"]
    assert plan.suppressed_by_mode == {}


def test_first_miss_starts_grace_without_deleting():
    """A newly-missing id under a healthy read starts grace, deletes next time."""
    grace: dict = {}
    streak: dict = {}
    plan = _plan(
        indexed_by_mode={HYB: {"9"}},
        nextcloud_file_ids=set(),
        discovered_by_mode={HYB: 1},  # healthy: something else was discovered
        attempted_modes={HYB},
        grace_state=grace,
        streak_state=streak,
        now=2000.0,
    )
    assert plan.to_delete == []
    assert grace == {(USER, "9", "file"): 2000.0}


def test_attempted_but_nothing_indexed_is_not_suppressed():
    """0 discovered with 0 indexed is not implausible -> no suppression, no streak."""
    grace: dict = {}
    streak: dict = {}
    plan = _plan(
        indexed_by_mode={},  # nothing indexed for this mode
        nextcloud_file_ids=set(),
        discovered_by_mode={},
        attempted_modes={HYB},
        grace_state=grace,
        streak_state=streak,
    )
    assert plan.to_delete == []
    assert plan.suppressed_by_mode == {}
    assert streak == {}


def test_bump_streak_evicts_oldest_when_bounded(monkeypatch):
    """The streak dict is bounded, evicting oldest-first like the consent backstop."""
    monkeypatch.setattr(scanner_module, "_EMPTY_DISCOVERY_STREAK_MAX", 4)
    streak: dict = {}
    # Fill to capacity with distinct keys.
    for i in range(4):
        _bump_streak(streak, (f"u{i}", HYB))
    assert len(streak) == 4
    # A new key triggers eviction down to half capacity (2) before inserting.
    _bump_streak(streak, ("u_new", HYB))
    assert len(streak) == 3  # 4 - (4 - 2) evicted, then +1 inserted
    assert ("u_new", HYB) in streak
    assert ("u0", HYB) not in streak  # oldest evicted first

    # Re-bumping an existing key never evicts and increments in place.
    before = dict(streak)
    _bump_streak(streak, ("u_new", HYB))
    assert streak[("u_new", HYB)] == before[("u_new", HYB)] + 1
    assert len(streak) == len(before)


# ---------------------------------------------------------------------------
# Deletion-tracking readback filter: keyed on acl_principals, not user_id, so a
# removed original-indexer's release converges instead of looping forever
# (blackbox-demo team-folder removal). Legacy points (no acl set) still tracked
# by user_id.
# ---------------------------------------------------------------------------


def test_indexed_files_filter_keys_on_acl_principal_not_user_id():
    """Primary branch selects by the reader's principal, scoped to files."""
    f = _indexed_files_scroll_filter("Demo-User")
    # doc_type=file is a hard requirement.
    assert any(
        isinstance(c, FieldCondition)
        and c.key == "doc_type"
        and c.match.value == "file"
        for c in f.must
    )
    # The principal (acl) branch — NOT a bare user_id match — is the primary one.
    acl_branch = next(
        c
        for c in f.should
        if isinstance(c, FieldCondition) and c.key == ACL_PRINCIPALS_KEY
    )
    assert acl_branch.match.any == [user_principal("Demo-User")]
    # No top-level user_id condition (that stamp is what caused the loop).
    assert not any(isinstance(c, FieldCondition) and c.key == "user_id" for c in f.must)


def test_indexed_files_filter_legacy_branch_is_user_id_scoped_to_empty_acl():
    """Legacy points (no acl_principals) fall back to user_id — but only then."""
    f = _indexed_files_scroll_filter("Demo-User")
    legacy = next(c for c in f.should if not isinstance(c, FieldCondition))
    keys = {
        c.key
        for c in legacy.must
        if isinstance(c, FieldCondition) and c.match is not None
    }
    assert "user_id" in keys  # legacy fallback keys on the indexer stamp
    # …but is gated on acl_principals being empty, so it can't re-loop modern pts.
    assert any(
        isinstance(c, IsEmptyCondition) and c.is_empty.key == ACL_PRINCIPALS_KEY
        for c in legacy.must
    )


# ---------------------------------------------------------------------------
# Suppressed-deletion emission (metric + log wiring, extracted for testability).
# ---------------------------------------------------------------------------


def test_record_suppressed_deletions_meters_only_nonzero_modes(monkeypatch):
    """Emit one counter increment per mode that actually suppressed; skip zeros."""
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        scanner_module,
        "record_vector_sync_deletions_suppressed",
        lambda mode, count: calls.append((mode, count)),
    )
    plan = _FileDeletionPlan(
        to_delete=[],
        suppressed_by_mode={HYB: 3, KW: 0},  # KW zero must be skipped
        streaks={HYB: 2},
    )
    _record_suppressed_deletions("SCAN-1", plan, {HYB: {"a", "b", "c"}}, threshold=3)
    assert calls == [(HYB, 3)]


def test_record_suppressed_deletions_noop_when_nothing_suppressed(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        scanner_module,
        "record_vector_sync_deletions_suppressed",
        lambda mode, count: calls.append((mode, count)),
    )
    plan = _FileDeletionPlan(to_delete=["x"], suppressed_by_mode={}, streaks={})
    _record_suppressed_deletions("SCAN-2", plan, {}, threshold=3)
    assert calls == []
