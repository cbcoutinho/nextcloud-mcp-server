"""Integration tests for verify-on-read access checks (ADR-019).

These tests exercise ``verify_search_results`` against a real Nextcloud
instance — the verification path's whole purpose is to consult Nextcloud as
the source of truth, so unit-level mocks don't catch protocol or status-code
mismatches between our verifier and the real API.

**Coverage**: the ``note`` verifier and the ``file`` verifier (tag-membership
gate, see the shared-recipient tests below) are exercised against real
Nextcloud here. The ``deck_card`` (Deck app) and ``news_item`` (News app)
verifiers are unit-tested with mocked HTTP responses in
``tests/unit/search/test_verification.py``. Adding integration coverage for
those types is tracked as a follow-up — it requires fixture data (a Deck board
with cards, a News feed) that is non-trivial to seed from CI. The mocked unit
tests are accurate for status-code semantics but won't catch payload-shape
regressions in those Nextcloud apps; the trade-off is documented here so
future readers know which suite owns which verifier.

Qdrant is mocked out (``delete_document_points`` and the payload-resolution
helpers) so these tests don't require a running vector database. The unit
suite in ``tests/unit/search/test_verification.py`` covers the Qdrant-side
behaviour separately.
"""

import logging
import os
import uuid

import pytest
from httpx import BasicAuth, HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.search import verification
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.verification import verify_search_results
from tests.integration.conftest import PDF_BYTES

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration


def _result_for_note(note_id: int) -> SearchResult:
    return SearchResult(
        id=str(note_id),
        doc_type="note",
        title=f"note_{note_id}",
        excerpt="...",
        score=0.9,
    )


def _result_for_file(file_id: int, path: str) -> SearchResult:
    # Mirrors what the algorithm layer propagates: doc_id IS the global file id,
    # stringified (SearchResult.id is always str), ``path`` is carried in
    # metadata (owner-relative) for log context only.
    return SearchResult(
        id=str(file_id),
        doc_type="file",
        title=path.split("/")[-1],
        excerpt="...",
        score=0.9,
        metadata={"path": path},
    )


def _user_client(username: str, password: str) -> NextcloudClient:
    return NextcloudClient(
        base_url=os.environ["NEXTCLOUD_HOST"],
        username=username,
        auth=BasicAuth(username, password),
        password=password,
    )


async def test_verify_keeps_accessible_note(
    nc_client: NextcloudClient, temporary_note: dict, mocker
):
    """A note that exists in Nextcloud must be kept by verification."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    note_id = temporary_note["id"]
    results = [_result_for_note(note_id)]

    kept, dropped_count = await verify_search_results(nc_client, results)

    assert [r.id for r in kept] == [str(note_id)]
    assert dropped_count == 0
    spy_evict.assert_not_awaited()


async def test_verify_drops_deleted_note_and_schedules_eviction(
    nc_client: NextcloudClient, mocker
):
    """The core ghost-record scenario.

    Create a note, delete it via the API (no webhook delivery), then run
    verification with a SearchResult still pointing at the gone-but-indexed
    document. verify-on-read must drop it and schedule eviction.
    """
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    # Create a note we'll delete to simulate a ghost record
    unique_suffix = uuid.uuid4().hex[:8]
    created = await nc_client.notes.create_note(
        title=f"verify-on-read ghost {unique_suffix}",
        content="This note will be deleted before verification runs.",
        category="VerifyOnReadTest",
    )
    note_id = created["id"]

    # Delete via API directly. In production a webhook *should* fire and
    # evict from Qdrant — but the whole point of ADR-019 is that we cannot
    # rely on this. Verification must catch the drift independently.
    await nc_client.notes.delete_note(note_id=note_id)

    # Confirm the note is really gone before running verification, so the
    # test fails fast if the API behaves unexpectedly.
    with pytest.raises(HTTPStatusError) as exc_info:
        await nc_client.notes.get_note(note_id)
    assert exc_info.value.response.status_code == 404

    kept, dropped_count = await verify_search_results(
        nc_client, [_result_for_note(note_id)]
    )

    assert kept == [], "deleted note must not pass verification"
    assert dropped_count == 1
    spy_evict.assert_awaited_once_with(str(note_id), "note", nc_client.username)


async def test_verify_mixed_accessible_and_deleted(
    nc_client: NextcloudClient, temporary_note: dict, mocker
):
    """Verification must drop only the inaccessible result, keep the rest."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    # temporary_note stays alive for the duration of the test.
    accessible_id = temporary_note["id"]

    # Make a second note and immediately delete it to create a ghost id.
    unique_suffix = uuid.uuid4().hex[:8]
    ghost = await nc_client.notes.create_note(
        title=f"verify-on-read ghost mix {unique_suffix}",
        content="ghost",
        category="VerifyOnReadTest",
    )
    ghost_id = ghost["id"]
    await nc_client.notes.delete_note(note_id=ghost_id)

    results = [
        _result_for_note(accessible_id),
        _result_for_note(ghost_id),
    ]
    kept, dropped_count = await verify_search_results(nc_client, results)

    assert [r.id for r in kept] == [str(accessible_id)]
    assert dropped_count == 1
    spy_evict.assert_awaited_once_with(str(ghost_id), "note", nc_client.username)


async def test_verify_dedupes_chunks_of_same_document(
    nc_client: NextcloudClient, temporary_note: dict, mocker
):
    """Multiple chunks of the same note must produce ONE Nextcloud round-trip."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    # Spy through to the real notes client to count round-trips
    real_get_note = nc_client.notes.get_note
    spy_get_note = mocker.AsyncMock(side_effect=real_get_note)
    mocker.patch.object(nc_client.notes, "get_note", spy_get_note)

    note_id = temporary_note["id"]
    # Three chunks of the same note (chunk_index varies)
    results = [
        SearchResult(
            id=str(note_id),
            doc_type="note",
            title="note",
            excerpt=f"chunk {i}",
            score=0.9 - i * 0.1,
            chunk_index=i,
        )
        for i in range(3)
    ]

    kept, dropped_count = await verify_search_results(nc_client, results)

    # All three chunks kept (they're all from the same accessible note)
    assert len(kept) == 3
    assert dropped_count == 0
    # ...but verification only fetched the note ONCE
    assert spy_get_note.await_count == 1


# ---------------------------------------------------------------------------
# File verifier — cross-user shared access (ACL-aware search, PR #813)
# ---------------------------------------------------------------------------
#
# These exercise the verifier fix that makes ACL-aware search actually work
# end-to-end: a file an owner shared with another user must survive
# verify-on-read for the *recipient*, even when it lives in a subfolder of the
# owner's tree (Nextcloud mounts received shares at the recipient's root by
# basename, so the owner-relative path does NOT resolve under the recipient's
# root). The fix verifies by global file id, which is ACL-aware.


@pytest.fixture
async def alice_bob_clients(test_users_setup):
    """Direct NextcloudClients for alice (owner) and bob (recipient)."""
    alice = _user_client("alice", test_users_setup["alice"]["password"])
    bob = _user_client("bob", test_users_setup["bob"]["password"])
    try:
        yield alice, bob
    finally:
        await alice._client.aclose()
        await bob._client.aclose()


async def test_verify_keeps_nested_file_shared_with_recipient(
    alice_bob_clients, mocker
):
    """The PR #813 acceptance check at the verifier layer, under tag-gating.

    Alice owns a PDF in a *subfolder*, tags it ``vector-index`` (userVisible),
    and shares it with Bob. Verifying the result as Bob must KEEP it — proving
    the tag REPORT surfaces an owner-assigned tag on a file shared into Bob's
    tree. If a future Nextcloud version stops surfacing the owner's tag to a
    recipient, this is where strict tag-gating regresses shared search.
    """
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    alice, bob = alice_bob_clients
    suffix = uuid.uuid4().hex[:8]
    test_dir = f"acl_verify_{suffix}"
    nested_dir = f"{test_dir}/reports"
    shared_path = f"{nested_dir}/shared.pdf"

    await alice.webdav.create_directory(test_dir)
    await alice.webdav.create_directory(nested_dir)
    await alice.webdav.write_file(shared_path, PDF_BYTES, "application/pdf")
    file_id = (await alice.webdav.get_file_info(shared_path))["id"]
    tag = await alice.webdav.get_or_create_tag(
        name=get_settings().vector_sync_pdf_tag,
        user_visible=True,
        user_assignable=True,
    )
    await alice.webdav.assign_tag_to_file(file_id, tag["id"])

    await alice.sharing.create_share(
        path=f"/{shared_path}", share_with="bob", share_type=0, permissions=1
    )

    try:
        kept, dropped_count = await verify_search_results(
            bob, [_result_for_file(file_id, shared_path)]
        )

        assert [r.id for r in kept] == [str(file_id)], (
            "a nested tagged PDF shared with bob must pass verification for bob"
        )
        assert dropped_count == 0
        spy_evict.assert_not_awaited()
    finally:
        try:
            await alice.webdav.remove_tag_from_file(file_id, tag["id"])
        except Exception:
            pass
        await alice.webdav.delete_resource(test_dir)


async def test_verify_drops_unshared_file_for_other_user(alice_bob_clients, mocker):
    """Negative control: a file Alice did NOT share is absent from Bob's
    vector-index tag set (and his tree), so it must be dropped + scheduled for
    eviction under his identity."""
    spy_evict = mocker.AsyncMock()
    mocker.patch.object(verification, "delete_document_points", spy_evict)

    alice, bob = alice_bob_clients
    suffix = uuid.uuid4().hex[:8]
    test_dir = f"acl_verify_priv_{suffix}"
    private_path = f"{test_dir}/private.pdf"

    await alice.webdav.create_directory(test_dir)
    await alice.webdav.write_file(private_path, PDF_BYTES, "application/pdf")
    file_id = (await alice.webdav.get_file_info(private_path))["id"]

    try:
        kept, dropped_count = await verify_search_results(
            bob, [_result_for_file(file_id, private_path)]
        )

        assert kept == [], "an unshared file must not pass verification for bob"
        assert dropped_count == 1
        spy_evict.assert_awaited_once_with(str(file_id), "file", bob.username)
    finally:
        await alice.webdav.delete_resource(test_dir)
