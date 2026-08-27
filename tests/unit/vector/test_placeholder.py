"""Unit tests for placeholder orphan-sweep (card #101).

When the per-tenant Pod OOMKills mid-batch the in-memory processor
queue is lost. Placeholder points written to Qdrant survive, and the
next Pod's scanner would skip them under the
``5 × VECTOR_SYNC_SCAN_INTERVAL`` staleness gate (~5h with the deployed
1h scan interval). ``sweep_orphan_placeholders`` runs once at Pod
startup and deletes any placeholder whose ``instance_id`` payload
doesn't match the current Pod-process, restoring throughput within
one scan cycle.

The sweep contract this file pins:

* placeholder with ``instance_id != _INSTANCE_ID`` → deleted
* placeholder with **absent** ``instance_id`` → deleted
  (back-compat for placeholders written by pre-fix Pod versions)
* placeholder with ``instance_id == _INSTANCE_ID`` → kept
* dead-letter marker (``dead_letter`` present, True OR False) → kept
* empty / no-results scroll → no-op (no spurious delete call)
* multi-page scroll → all pages visited, deletes batched per page
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.vector import placeholder as placeholder_module
from nextcloud_mcp_server.vector.placeholder import sweep_orphan_placeholders


def _scrolled_point(point_id, instance_id=...):
    """Stand-in for a qdrant_client Record. Sweep reads only
    ``id`` and ``payload``; passing ``instance_id=...`` (Ellipsis)
    omits the field entirely (the back-compat case)."""
    payload: dict = {"is_placeholder": True}
    if instance_id is not ...:
        payload["instance_id"] = instance_id
    return SimpleNamespace(id=point_id, payload=payload)


def _make_client(scroll_pages):
    """Build an AsyncMock qdrant client whose ``scroll`` returns the
    given ``(points, next_offset)`` pages in order, then mimics the
    real client's exhausted-cursor return of ``([], None)``."""
    client = AsyncMock()
    pages = list(scroll_pages) + [([], None)]
    client.scroll.side_effect = pages
    return client


@pytest.mark.unit
async def test_sweeps_orphan_with_different_instance_id(monkeypatch):
    monkeypatch.setattr(placeholder_module, "_INSTANCE_ID", "pod-new")
    client = _make_client(
        [
            ([_scrolled_point("p1", instance_id="pod-old")], None),
        ]
    )

    swept, kept = await sweep_orphan_placeholders(client, "nextcloud_content")

    assert (swept, kept) == (1, 0)
    client.delete.assert_awaited_once()
    # Selector is the orphan's point id — own-Pod placeholders never
    # reach the delete call.
    assert client.delete.await_args.kwargs["points_selector"] == ["p1"]


@pytest.mark.unit
async def test_sweeps_placeholder_with_absent_instance_id(monkeypatch):
    """Back-compat: placeholders written by pre-fix Pod versions have
    no ``instance_id`` field. They MUST be treated as orphans so the
    first deploy of this fix doesn't leave old placeholders behind."""
    monkeypatch.setattr(placeholder_module, "_INSTANCE_ID", "pod-new")
    client = _make_client(
        [
            ([_scrolled_point("legacy", instance_id=...)], None),
        ]
    )

    swept, kept = await sweep_orphan_placeholders(client, "nextcloud_content")

    assert (swept, kept) == (1, 0)
    assert client.delete.await_args.kwargs["points_selector"] == ["legacy"]


@pytest.mark.unit
async def test_keeps_own_pod_placeholders(monkeypatch):
    """A surviving Pod's own placeholders must NOT be deleted —
    that's what the staleness gate is for, and the processor may
    still be working through them."""
    monkeypatch.setattr(placeholder_module, "_INSTANCE_ID", "pod-current")
    client = _make_client(
        [
            (
                [
                    _scrolled_point("mine-1", instance_id="pod-current"),
                    _scrolled_point("mine-2", instance_id="pod-current"),
                ],
                None,
            ),
        ]
    )

    swept, kept = await sweep_orphan_placeholders(client, "nextcloud_content")

    assert (swept, kept) == (0, 2)
    client.delete.assert_not_awaited()


@pytest.mark.unit
async def test_keeps_dead_letter_markers(monkeypatch):
    """Dead-letter markers (vector/dead_letter.py) reuse is_placeholder=True for
    the search exclusion but are DURABLE terminal-state records, not in-flight
    placeholders. They carry a foreign/absent instance_id, so without the
    carve-out the sweep would delete them on every Pod restart and the
    dead-lettered document would loop again. They MUST be kept."""
    monkeypatch.setattr(placeholder_module, "_INSTANCE_ID", "pod-new")
    marker = SimpleNamespace(
        id="dl-1",
        payload={"is_placeholder": True, "dead_letter": True, "instance_id": "pod-old"},
    )
    client = _make_client([([marker], None)])

    swept, kept = await sweep_orphan_placeholders(client, "nextcloud_content")

    assert (swept, kept) == (0, 1)
    client.delete.assert_not_awaited()


@pytest.mark.unit
async def test_keeps_soft_dead_letter_markers(monkeypatch):
    """A SOFT marker (``dead_letter=False``) must survive the sweep too.

    GH #1345 added a consecutive-index-failure counter carried on the same
    marker point, which stays ``dead_letter=False`` until the failure count
    reaches the limit. The carve-out therefore keys off the field's PRESENCE,
    not its value — sweeping a soft marker would reset the failure budget on
    every Pod restart and restore the infinite retry loop the counter exists to
    stop.

    This is the case a future refactor is most likely to break, because
    ``dead_letter is True`` reads more intuitively than ``is not None``.
    """
    monkeypatch.setattr(placeholder_module, "_INSTANCE_ID", "pod-new")
    marker = SimpleNamespace(
        id="dl-soft-1",
        payload={
            "is_placeholder": True,
            "dead_letter": False,
            "attempts": 2,
            "instance_id": "pod-old",
        },
    )
    client = _make_client([([marker], None)])

    swept, kept = await sweep_orphan_placeholders(client, "nextcloud_content")

    assert (swept, kept) == (0, 1)
    client.delete.assert_not_awaited()


@pytest.mark.unit
async def test_noop_when_no_placeholders_exist(monkeypatch):
    """Cold-boot tenant with an empty collection — sweep does NOT
    issue a delete request, so a trivially-empty batch can't trip
    Qdrant validation on an empty selector list."""
    monkeypatch.setattr(placeholder_module, "_INSTANCE_ID", "pod-fresh")
    client = _make_client([([], None)])

    swept, kept = await sweep_orphan_placeholders(client, "nextcloud_content")

    assert (swept, kept) == (0, 0)
    client.delete.assert_not_awaited()


@pytest.mark.unit
async def test_walks_multiple_scroll_pages(monkeypatch):
    """Scroll cursor exhaustion is signalled by ``offset is None`` per
    Qdrant's contract. Sweep must keep paging until the cursor is
    exhausted, batching the delete per page."""
    monkeypatch.setattr(placeholder_module, "_INSTANCE_ID", "pod-current")
    client = _make_client(
        [
            (
                [
                    _scrolled_point("orphan-1", instance_id="pod-prev"),
                    _scrolled_point("mine", instance_id="pod-current"),
                ],
                "next-cursor",
            ),
            (
                [_scrolled_point("orphan-2", instance_id=...)],
                None,
            ),
        ]
    )

    swept, kept = await sweep_orphan_placeholders(
        client, "nextcloud_content", batch_size=2
    )

    assert (swept, kept) == (2, 1)
    # One delete per page (each page had at least one orphan).
    assert client.delete.await_count == 2
    delete_selectors = [
        call.kwargs["points_selector"] for call in client.delete.await_args_list
    ]
    assert delete_selectors == [["orphan-1"], ["orphan-2"]]


@pytest.mark.unit
async def test_write_placeholder_payload_includes_instance_id(monkeypatch):
    """Pin the new payload contract: ``write_placeholder_point`` MUST
    stamp the current Pod's ``_INSTANCE_ID`` onto the payload it
    upserts, so the next Pod's sweep can identify these placeholders
    as belonging to a different process."""
    monkeypatch.setattr(placeholder_module, "_INSTANCE_ID", "pod-pinned")

    fake_qdrant = AsyncMock()
    fake_settings = SimpleNamespace(
        get_collection_name=lambda: "nextcloud_content",
    )
    fake_embedding = SimpleNamespace(get_dimension=lambda: 4)

    async def fake_get_qdrant_client():
        return fake_qdrant

    monkeypatch.setattr(placeholder_module, "get_qdrant_client", fake_get_qdrant_client)
    monkeypatch.setattr(placeholder_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(placeholder_module, "get_provider", lambda: fake_embedding)

    await placeholder_module.write_placeholder_point(
        doc_id="d-42",
        doc_type="note",
        user_id="alice",
        modified_at=1700000000,
        etag="abc",
    )

    fake_qdrant.upsert.assert_awaited_once()
    upserted_point = fake_qdrant.upsert.await_args.kwargs["points"][0]
    assert upserted_point.payload["instance_id"] == "pod-pinned"
    # Sanity: the other contract-pinning fields are still emitted.
    assert upserted_point.payload["is_placeholder"] is True
    assert upserted_point.payload["status"] == "pending"
    # Dense slot is always sized from the embedding provider (dim=4 here) — the
    # keyword/simple-dimension branch is gone (per-document index mode).
    assert len(upserted_point.vector["dense"]) == 4


# --- query_document_metadata: placeholder vs real must not be a coin flip ---
#
# Placeholder and chunk points have different deterministic ids, so they coexist.
# The lookup used to be one unfiltered scroll(limit=1) with no ordering, so which
# one answered "is this indexed?" was decided by point-id order. The scanner tests
# `is_placeholder` BEFORE `index_mode`/`modified_at`, so an indexed, unchanged
# document whose leftover placeholder sorted first read as "still queued", went
# stale, and was re-queued every cycle — a full re-OCR each time on the OCR tier.


def _payload(*, is_placeholder: bool, modified_at: int) -> dict:
    return {
        "is_placeholder": is_placeholder,
        "modified_at": modified_at,
        "doc_id": "d-1",
    }


def _patch_lookup(monkeypatch, *, real: dict | None, placeholder: dict | None):
    """Serve each scroll from its ``is_placeholder`` filter value, so the test
    pins the resolution logic rather than any incidental call order."""
    fake_qdrant = AsyncMock()

    async def scroll(**kwargs):
        wanted = next(
            c.match.value
            for c in kwargs["scroll_filter"].must
            if c.key == "is_placeholder"
        )
        found = placeholder if wanted else real
        return ([SimpleNamespace(id="p", payload=found)] if found else [], None)

    fake_qdrant.scroll.side_effect = scroll

    async def fake_get_qdrant_client():
        return fake_qdrant

    monkeypatch.setattr(placeholder_module, "get_qdrant_client", fake_get_qdrant_client)
    monkeypatch.setattr(
        placeholder_module,
        "get_settings",
        lambda: SimpleNamespace(get_collection_name=lambda: "c"),
    )
    return fake_qdrant


@pytest.mark.unit
async def test_leftover_placeholder_does_not_mask_the_index(monkeypatch):
    """THE regression: same modified_at ⇒ the index answers, not the leftover.

    A placeholder no newer than the indexed content is stale bookkeeping. If it
    answered here the scanner would take its `is_placeholder` branch and re-queue
    a document that is already correctly indexed — forever.
    """
    _patch_lookup(
        monkeypatch,
        real=_payload(is_placeholder=False, modified_at=1700),
        placeholder=_payload(is_placeholder=True, modified_at=1700),
    )

    got = await placeholder_module.query_document_metadata(
        doc_id="d-1", doc_type="file", user_id="alice"
    )

    assert got["is_placeholder"] is False


@pytest.mark.unit
async def test_in_flight_newer_version_still_wins(monkeypatch):
    """A genuinely newer queued version must keep suppressing re-enqueues.

    This is why the fix is not simply "always prefer the real point": while a
    newer version is in flight, returning the stale indexed point would make the
    scanner re-queue an already-queued document on every pass.
    """
    _patch_lookup(
        monkeypatch,
        real=_payload(is_placeholder=False, modified_at=1700),
        placeholder=_payload(is_placeholder=True, modified_at=1800),
    )

    got = await placeholder_module.query_document_metadata(
        doc_id="d-1", doc_type="file", user_id="alice"
    )

    assert got["is_placeholder"] is True


@pytest.mark.unit
async def test_placeholder_only_and_real_only_and_neither(monkeypatch):
    """The three single-sided cases keep their pre-existing answers."""
    _patch_lookup(
        monkeypatch,
        real=None,
        placeholder=_payload(is_placeholder=True, modified_at=1700),
    )
    got = await placeholder_module.query_document_metadata(
        doc_id="d-1", doc_type="file", user_id="alice"
    )
    assert got["is_placeholder"] is True  # queued, never indexed

    _patch_lookup(
        monkeypatch,
        real=_payload(is_placeholder=False, modified_at=1700),
        placeholder=None,
    )
    got = await placeholder_module.query_document_metadata(
        doc_id="d-1", doc_type="file", user_id="alice"
    )
    assert got["is_placeholder"] is False  # indexed, nothing queued

    _patch_lookup(monkeypatch, real=None, placeholder=None)
    got = await placeholder_module.query_document_metadata(
        doc_id="d-1", doc_type="file", user_id="alice"
    )
    assert got is None  # never seen


@pytest.mark.unit
async def test_lookup_failure_still_fails_open(monkeypatch):
    """Unchanged contract: a Qdrant error reads as None ("never seen").

    Worth pinning now that the two scrolls run under an anyio task group: a child
    failure surfaces as an ExceptionGroup, not the raw error. That is still an
    Exception (3.11+), so the fail-open handler catches it — but silently, which
    is exactly the kind of thing that stops being true after a refactor.
    """
    fake_qdrant = AsyncMock()
    fake_qdrant.scroll.side_effect = RuntimeError("qdrant down")

    async def fake_get_qdrant_client():
        return fake_qdrant

    monkeypatch.setattr(placeholder_module, "get_qdrant_client", fake_get_qdrant_client)
    monkeypatch.setattr(
        placeholder_module,
        "get_settings",
        lambda: SimpleNamespace(get_collection_name=lambda: "c"),
    )

    got = await placeholder_module.query_document_metadata(
        doc_id="d-1", doc_type="file", user_id="alice"
    )

    assert got is None
