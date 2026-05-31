"""NATS ingest producer: DocumentTask → IngestMessage + dedup header (§3.4)."""

import hashlib
import json
from pathlib import Path

import pytest

from nextcloud_mcp_server.canonical import canonical_json
from nextcloud_mcp_server.vector.queue.factory import _transport_for
from nextcloud_mcp_server.vector.queue.nats import (
    NatsTaskProducer,
    _modified_at_rfc3339,
    msg_id,
    warn_if_insecure_nats_url,
)
from nextcloud_mcp_server.vector.queue.postgres import PostgresTaskProducer
from nextcloud_mcp_server.vector.scanner import DocumentTask

FIXTURE = Path(__file__).parents[2] / "fixtures" / "ingest_message_example.json"
TENANT = "00000000-0000-0000-0000-000000000001"


def _producer(mocker, tenant_id=TENANT):
    return NatsTaskProducer(
        nc=mocker.MagicMock(), js=mocker.AsyncMock(), tenant_id=tenant_id
    )


def test_ingest_message_translation(mocker):
    p = _producer(mocker)
    task = DocumentTask(
        user_id="alice",
        doc_id="12345",
        doc_type="file",
        operation="index",
        modified_at=1700000000,
        file_path="/Documents/report.pdf",
        etag="etag-abc123",
    )
    msg = p.ingest_message(task)
    assert msg["tenant_id"] == TENANT  # from settings, not the task
    assert msg["content_hash"] == "etag-abc123"  # etag wins
    assert msg["user_id"] == "alice"
    assert msg["doc_type"] == "file"
    assert msg["operation"] == "index"
    assert msg["file_path"] == "/Documents/report.pdf"


def test_content_hash_falls_back_to_modified_at(mocker):
    p = _producer(mocker)
    task = DocumentTask(
        user_id="u", doc_id="d", doc_type="note", operation="delete", modified_at=0
    )
    assert p.ingest_message(task)["content_hash"] == "0"


async def test_send_publishes_with_dedup_header(mocker):
    p = _producer(mocker)
    task = DocumentTask(
        user_id="alice",
        doc_id="12345",
        doc_type="file",
        operation="index",
        modified_at=1700000000,
        etag="e",
    )
    await p.send(task)
    p._js.publish.assert_awaited_once()
    args = p._js.publish.await_args.args
    kwargs = p._js.publish.await_args.kwargs
    assert args[0] == f"mcp.ingest.requested.{TENANT}"
    expected_mid = msg_id(TENANT, "12345", _modified_at_rfc3339(1700000000))
    assert kwargs["headers"]["Nats-Msg-Id"] == expected_mid
    assert json.loads(args[1])["doc_id"] == "12345"


def test_msg_id_known_vector():
    mid = msg_id("t", "d", "2026-01-01T00:00:00+00:00")
    expected = hashlib.sha256(
        canonical_json(
            {
                "tenant_id": "t",
                "doc_id": "d",
                "modified_at": "2026-01-01T00:00:00+00:00",
            }
        )
    ).hexdigest()
    assert mid == expected


def test_publisher_matches_shared_fixture(mocker):
    # The same fixture is validated as an IngestMessage in the processor repo.
    # Here we assert the publisher emits exactly the fixture's key set + stable
    # field values (modified_at format is allowed to differ — epoch→ISO).
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    p = _producer(mocker, tenant_id=fixture["tenant_id"])
    task = DocumentTask(
        user_id=fixture["user_id"],
        doc_id=fixture["doc_id"],
        doc_type=fixture["doc_type"],
        operation=fixture["operation"],
        modified_at=1764201600,
        file_path=fixture["file_path"],
        etag=fixture["content_hash"],
    )
    msg = p.ingest_message(task)
    assert set(msg.keys()) == set(fixture.keys())
    for key in (
        "tenant_id",
        "doc_id",
        "content_hash",
        "doc_type",
        "operation",
        "user_id",
        "file_path",
    ):
        assert msg[key] == fixture[key]
    assert msg["modified_at"]  # non-empty ISO timestamp


@pytest.mark.parametrize(
    "url,expected",
    [
        ("nats://nats:4222", "nats"),
        ("postgres://h/db", "postgres"),
        ("postgresql://h/db", "postgres"),
        ("https://elsewhere", "nats"),
    ],
)
def test_transport_for(url, expected):
    assert _transport_for(url) == expected


async def test_postgres_producer_is_a_seam():
    with pytest.raises(NotImplementedError, match="documented seam"):
        await PostgresTaskProducer.connect(object())


@pytest.mark.parametrize(
    "url,should_warn",
    [
        ("nats://nats:4222", True),
        ("ws://nats:8080", True),
        ("tls://nats:4222", False),
        ("wss://nats:8080", False),
    ],
)
def test_warn_if_insecure_nats_url(url, should_warn, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        warn_if_insecure_nats_url(url)
    warned = any("unencrypted transport" in r.getMessage() for r in caplog.records)
    assert warned is should_warn
