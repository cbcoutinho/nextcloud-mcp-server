"""Unit tests for the mail-message scanner (initial-sync path).

The incremental path depends on live Qdrant lookups (``_scroll_doc_ids`` /
``query_document_metadata``); these tests cover the initial-sync enumeration —
accounts → mailboxes → newest-N messages — which is the bulk of the new logic
and needs no Qdrant.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.vector import scanner as scanner_module
from nextcloud_mcp_server.vector.scanner import DocumentTask, scan_mail_messages

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_scanner_module_state():
    """Isolate the module-global grace-period / cap-log dicts per test."""
    scanner_module._potentially_deleted.clear()
    scanner_module._mail_cap_logged.clear()
    scanner_module._empty_discovery_streak.clear()
    yield
    scanner_module._potentially_deleted.clear()
    scanner_module._mail_cap_logged.clear()
    scanner_module._empty_discovery_streak.clear()


def _patch_incremental(mocker, *, indexed_ids, existing_metadata, interval=1):
    """Patch the Qdrant-facing helpers for the incremental scan path."""
    mocker.patch.object(scanner_module, "get_qdrant_client", new=AsyncMock())
    mocker.patch.object(
        scanner_module,
        "_scroll_doc_ids",
        new=AsyncMock(return_value={str(doc_id) for doc_id in indexed_ids}),
    )
    mocker.patch.object(
        scanner_module,
        "query_document_metadata",
        new=AsyncMock(return_value=existing_metadata),
    )
    mocker.patch.object(scanner_module, "write_placeholder_point", new=AsyncMock())
    mocker.patch.object(scanner_module, "record_vector_sync_scan")
    mocker.patch.object(
        scanner_module,
        "get_settings",
        return_value=MagicMock(vector_sync_scan_interval=interval),
    )


class _CollectingStream:
    """Minimal TaskProducer stand-in that records sent DocumentTasks."""

    def __init__(self) -> None:
        self.tasks: list[DocumentTask] = []

    async def send(self, task: DocumentTask) -> None:
        self.tasks.append(task)


async def test_initial_sync_enumerates_accounts_mailboxes_messages(mocker):
    nc_client = MagicMock()
    nc_client.mail.list_accounts = AsyncMock(return_value=[{"id": 1}])
    nc_client.mail.get_mailboxes = AsyncMock(
        return_value=[{"databaseId": 10}, {"databaseId": 11}]
    )

    async def list_messages(mailbox_id, *, limit, search_filter, view):
        if mailbox_id == 10:
            return [
                {"databaseId": 100, "dateInt": 1700000000},
                {"databaseId": 101, "dateInt": 1700000001},
            ]
        return [{"databaseId": 200, "dateInt": 1700000002}]

    nc_client.mail.list_messages = AsyncMock(side_effect=list_messages)

    placeholder = mocker.patch.object(
        scanner_module, "write_placeholder_point", new=AsyncMock()
    )
    mocker.patch.object(scanner_module, "record_vector_sync_scan")

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=True,
        scan_id=1,
    )

    assert queued == 3
    assert len(stream.tasks) == 3
    # All are mail_message index tasks carrying account/mailbox metadata.
    assert {t.doc_id for t in stream.tasks} == {"100", "101", "200"}
    assert all(t.doc_type == "mail_message" for t in stream.tasks)
    assert all(t.operation == "index" for t in stream.tasks)
    t100 = next(t for t in stream.tasks if t.doc_id == "100")
    assert t100.modified_at == 1700000000
    assert t100.metadata == {"account_id": 1, "mailbox_id": 10}
    # A placeholder is written per message before queueing.
    assert placeholder.await_count == 3
    # The shared index window is used: per-mailbox cap, no filter, and the
    # singleton view (threaded would hide every reply — see list_index_window).
    nc_client.mail.list_messages.assert_any_await(
        10,
        limit=scanner_module.MAIL_SCAN_MAX_PER_MAILBOX,
        search_filter=None,
        view="singleton",
    )


async def test_initial_sync_skips_mailbox_on_list_error(mocker):
    """A failing mailbox is logged and skipped; other mailboxes still index."""
    nc_client = MagicMock()
    nc_client.mail.list_accounts = AsyncMock(return_value=[{"id": 1}])
    nc_client.mail.get_mailboxes = AsyncMock(
        return_value=[{"databaseId": 10}, {"databaseId": 11}]
    )

    async def list_messages(mailbox_id, *, limit, search_filter, view):
        if mailbox_id == 10:
            raise RuntimeError("imap hiccup")
        return [{"databaseId": 200, "dateInt": 1700000002}]

    nc_client.mail.list_messages = AsyncMock(side_effect=list_messages)
    mocker.patch.object(scanner_module, "write_placeholder_point", new=AsyncMock())
    mocker.patch.object(scanner_module, "record_vector_sync_scan")

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=True,
        scan_id=1,
    )

    assert queued == 1
    assert {t.doc_id for t in stream.tasks} == {"200"}


async def test_no_accounts_queues_nothing(mocker):
    nc_client = MagicMock()
    nc_client.mail.list_accounts = AsyncMock(return_value=[])
    mocker.patch.object(scanner_module, "write_placeholder_point", new=AsyncMock())
    mocker.patch.object(scanner_module, "record_vector_sync_scan")

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=True,
        scan_id=1,
    )

    assert queued == 0
    assert stream.tasks == []


def _single_message_client(messages):
    nc_client = MagicMock()
    nc_client.mail.list_accounts = AsyncMock(return_value=[{"id": 1}])
    nc_client.mail.get_mailboxes = AsyncMock(return_value=[{"databaseId": 10}])
    nc_client.mail.list_messages = AsyncMock(return_value=messages)
    return nc_client


async def test_incremental_new_message_queued(mocker):
    """A message absent from Qdrant (no existing metadata) is queued to index."""
    _patch_incremental(mocker, indexed_ids=[], existing_metadata=None)
    nc_client = _single_message_client([{"databaseId": 100, "dateInt": 1700000000}])

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    assert queued == 1
    assert [(t.doc_id, t.operation) for t in stream.tasks] == [("100", "index")]


async def test_incremental_reappeared_message_clears_grace(mocker):
    """A message back in Nextcloud is removed from the deletion grace period."""
    # Already indexed and up-to-date, so it won't be re-queued.
    _patch_incremental(
        mocker, indexed_ids=["100"], existing_metadata={"modified_at": 1700000000}
    )
    scanner_module._potentially_deleted[("alice", "100", "mail_message")] = 123.0
    nc_client = _single_message_client([{"databaseId": 100, "dateInt": 1700000000}])

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    assert queued == 0
    assert stream.tasks == []
    assert ("alice", "100", "mail_message") not in scanner_module._potentially_deleted


async def test_incremental_deletes_after_grace_period(mocker):
    """An indexed message gone from Nextcloud past the grace period is deleted."""
    _patch_incremental(
        mocker, indexed_ids=["999"], existing_metadata={"modified_at": 1700000000}
    )
    # Seed the grace period far in the past so the delta exceeds grace_period.
    scanner_module._potentially_deleted[("alice", "999", "mail_message")] = 0.0
    # The mailbox still lists other (already up-to-date) mail, so 999 is
    # genuinely missing rather than the whole listing having failed.
    nc_client = _single_message_client([{"databaseId": 100, "dateInt": 1700000000}])

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    assert queued == 1
    assert [(t.doc_id, t.operation) for t in stream.tasks] == [("999", "delete")]
    assert (
        "alice",
        "999",
        "mail_message",
    ) not in scanner_module._potentially_deleted


async def test_incremental_first_missing_starts_grace(mocker):
    """A newly-missing indexed message enters the grace period (no delete yet)."""
    _patch_incremental(
        mocker, indexed_ids=["999"], existing_metadata={"modified_at": 1700000000}
    )
    # Not previously seen as missing; the mailbox still lists other mail.
    nc_client = _single_message_client([{"databaseId": 100, "dateInt": 1700000000}])

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    # First miss only starts the grace period — nothing queued, nothing deleted.
    assert queued == 0
    assert stream.tasks == []
    assert ("alice", "999", "mail_message") in scanner_module._potentially_deleted


async def test_empty_listing_skips_deletion_pass(mocker):
    """An all-empty listing while an index exists must not evict anything.

    Every mailbox listing failing (Mail app down, mailboxes not yet cached) is
    indistinguishable in the response from a genuinely emptied account, so the
    scanner declines to delete rather than wiping a user's whole mail index on a
    transient outage.
    """
    _patch_incremental(mocker, indexed_ids=["999"], existing_metadata=None)
    # Well past the grace period — only the empty-listing guard holds it back.
    scanner_module._potentially_deleted[("alice", "999", "mail_message")] = 0.0
    nc_client = _single_message_client([])

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    assert queued == 0
    assert stream.tasks == []
    # Still mid-grace: a later scan that lists successfully can still delete it.
    assert ("alice", "999", "mail_message") in scanner_module._potentially_deleted


async def test_partial_listing_failure_skips_deletion_pass(mocker):
    """One mailbox failing must not evict the messages indexed from it.

    The all-empty guard does not cover this: the surviving mailbox makes
    ``message_count`` nonzero, so without tracking the failure the scanner would
    read "mailbox 11's messages are gone" from what is really a transient error
    on that one mailbox.
    """
    _patch_incremental(mocker, indexed_ids=["999"], existing_metadata=None)
    # Well past the grace period — only the incomplete-listing guard holds it back.
    scanner_module._potentially_deleted[("alice", "999", "mail_message")] = 0.0

    nc_client = MagicMock()
    nc_client.mail.list_accounts = AsyncMock(return_value=[{"id": 1}])
    nc_client.mail.get_mailboxes = AsyncMock(
        return_value=[{"databaseId": 10}, {"databaseId": 11}]
    )

    async def _list_messages(mailbox_id, **kwargs):
        if mailbox_id == 11:
            raise RuntimeError("mailbox 11 is not cached")
        return [{"databaseId": 100, "dateInt": 1700000000}]

    nc_client.mail.list_messages = AsyncMock(side_effect=_list_messages)

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    # 100 is queued (no stored metadata), but 999 is NOT evicted.
    assert queued == 1
    assert [(t.doc_id, t.operation) for t in stream.tasks] == [("100", "index")]
    assert ("alice", "999", "mail_message") in scanner_module._potentially_deleted


async def test_empty_listing_with_empty_index_is_not_guarded(mocker):
    """Nothing indexed yet + nothing listed is the normal empty case, not a fault."""
    _patch_incremental(mocker, indexed_ids=[], existing_metadata=None)
    nc_client = _single_message_client([])

    stream = _CollectingStream()
    queued = await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    assert queued == 0
    assert stream.tasks == []


async def test_grace_key_isolated_by_doc_type(mocker):
    """A mail message reappearing must not clear a same-id note's grace period.

    Regression for Deck #376: the grace-period key includes doc_type, so a
    note 42 and a mail_message 42 for one user no longer collide.
    """
    # Mail message 42 is indexed and up-to-date (so it isn't re-queued), and is
    # present in Nextcloud (so the reappeared-clear path runs for mail).
    _patch_incremental(
        mocker, indexed_ids=["42"], existing_metadata={"modified_at": 1700000000}
    )
    # A note 42 is mid-grace-period for the same user.
    scanner_module._potentially_deleted[("alice", "42", "note")] = 123.0
    nc_client = _single_message_client([{"databaseId": 42, "dateInt": 1700000000}])

    stream = _CollectingStream()
    await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    # The note's grace entry is untouched by the mail scan (no cross-type stomp).
    assert ("alice", "42", "note") in scanner_module._potentially_deleted


async def test_filtered_scan_uses_the_resolved_tag_filter(mocker):
    """With MAIL_INDEX_TAG set, only tagged messages are listed."""
    _patch_incremental(mocker, indexed_ids=[], existing_metadata=None)
    mocker.patch.object(
        scanner_module,
        "mail_index_filter",
        new=AsyncMock(return_value="tags:7"),
    )
    nc_client = _single_message_client([{"databaseId": 100, "dateInt": 1700000000}])

    stream = _CollectingStream()
    await scan_mail_messages(
        user_id="alice",
        send_stream=stream,
        nc_client=nc_client,
        initial_sync=False,
        scan_id=1,
    )

    nc_client.mail.list_messages.assert_awaited_once_with(
        10,
        limit=scanner_module.MAIL_SCAN_MAX_PER_MAILBOX,
        search_filter="tags:7",
        view="singleton",
    )


async def test_filter_resolution_failure_aborts_before_the_deletion_pass(mocker):
    """Fail-closed: a tags-endpoint failure must not evict, nor index everything.

    Fail-open here would enrol the user's whole mailbox on a transient 500; the
    raise happens before the deletion pass, so nothing is evicted either.
    """
    _patch_incremental(mocker, indexed_ids=["999"], existing_metadata=None)
    scanner_module._potentially_deleted[("alice", "999", "mail_message")] = 0.0
    mocker.patch.object(
        scanner_module,
        "mail_index_filter",
        new=AsyncMock(side_effect=RuntimeError("tags endpoint down")),
    )
    nc_client = _single_message_client([{"databaseId": 100, "dateInt": 1700000000}])

    stream = _CollectingStream()
    with pytest.raises(RuntimeError):
        await scan_mail_messages(
            user_id="alice",
            send_stream=stream,
            nc_client=nc_client,
            initial_sync=False,
            scan_id=1,
        )

    assert stream.tasks == []
    nc_client.mail.list_messages.assert_not_awaited()
