"""Unit tests for the mail-message scanner (initial-sync path).

The incremental path depends on live Qdrant lookups (``_scroll_all_points`` /
``query_document_metadata``); these tests cover the initial-sync enumeration —
accounts → mailboxes → newest-N messages — which is the bulk of the new logic
and needs no Qdrant.
"""

from types import SimpleNamespace
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
        "_scroll_all_points",
        new=AsyncMock(
            return_value=[
                SimpleNamespace(payload={"doc_id": doc_id}) for doc_id in indexed_ids
            ]
        ),
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

    async def list_messages(mailbox_id, *, limit):
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
    # The per-mailbox cap is passed through.
    nc_client.mail.list_messages.assert_any_await(
        10, limit=scanner_module.MAIL_SCAN_MAX_PER_MAILBOX
    )


async def test_initial_sync_skips_mailbox_on_list_error(mocker):
    """A failing mailbox is logged and skipped; other mailboxes still index."""
    nc_client = MagicMock()
    nc_client.mail.list_accounts = AsyncMock(return_value=[{"id": 1}])
    nc_client.mail.get_mailboxes = AsyncMock(
        return_value=[{"databaseId": 10}, {"databaseId": 11}]
    )

    async def list_messages(mailbox_id, *, limit):
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
    _patch_incremental(mocker, indexed_ids=["999"], existing_metadata=None)
    # Seed the grace period far in the past so the delta exceeds grace_period.
    scanner_module._potentially_deleted[("alice", "999", "mail_message")] = 0.0
    # Mailbox now returns no messages, so 999 is missing.
    nc_client = _single_message_client([])

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
    _patch_incremental(mocker, indexed_ids=["999"], existing_metadata=None)
    # Not previously seen as missing, and the mailbox now returns no messages.
    nc_client = _single_message_client([])

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
