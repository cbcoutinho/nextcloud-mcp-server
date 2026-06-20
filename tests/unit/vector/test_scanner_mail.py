"""Unit tests for the mail-message scanner (initial-sync path).

The incremental path depends on live Qdrant lookups (``_scroll_all_points`` /
``query_document_metadata``); these tests cover the initial-sync enumeration —
accounts → mailboxes → newest-N messages — which is the bulk of the new logic
and needs no Qdrant.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.vector import scanner as scanner_module
from nextcloud_mcp_server.vector.scanner import DocumentTask, scan_mail_messages

pytestmark = pytest.mark.unit


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
