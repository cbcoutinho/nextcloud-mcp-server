"""Unit tests for the ``/webhooks/nextcloud`` HTTP receiver.

Builds a minimal Starlette app around ``handle_nextcloud_webhook`` so we can
drive it with ``TestClient`` without standing up the full FastMCP server.
"""

import anyio
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.vector import webhook_receiver
from nextcloud_mcp_server.vector.webhook_receiver import handle_nextcloud_webhook

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_warned_flag():
    """The receiver warns once per process when WEBHOOK_SECRET is missing.
    Reset between tests so each gets a clean slate."""
    webhook_receiver._warned_about_missing_secret = False
    yield
    webhook_receiver._warned_about_missing_secret = False


def _patch_secret(monkeypatch, secret: str | None) -> None:
    """Make ``get_settings()`` (as called inside the receiver) return a
    Settings instance with the given ``webhook_secret``."""
    monkeypatch.setattr(
        webhook_receiver,
        "get_settings",
        lambda: Settings(webhook_secret=secret),
    )


def _make_app(send_stream=None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/webhooks/nextcloud", handle_nextcloud_webhook, methods=["POST"])
        ]
    )
    app.state.document_send_stream = send_stream
    return app


_NOTE_CREATED = {
    "user": {"uid": "admin", "displayName": "admin"},
    "time": 1762850245,
    "event": {
        "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        "node": {
            "id": 437,
            "path": "/admin/files/Notes/Webhooks/Webhook Test Note.md",
        },
    },
}


_NOTE_DELETED = {
    "user": {"uid": "alice"},
    "time": 1762851093,
    "event": {
        "class": "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent",
        "node": {"id": 99, "path": "/alice/files/Notes/foo.md"},
    },
}


# Deck PR #7910 (CardCreatedEvent etc.) emits ``{"card": Card::jsonSerialize()}``
# — see ~/Software/deck/lib/Event/ACardEvent.php. Card::jsonSerialize() includes
# id and stackId but not boardId (the processor falls back to iteration for
# that). BoardUpdatedEvent emits only ``{"boardId": int}``.
_DECK_CARD_CREATED = {
    "user": {"uid": "admin"},
    "time": 1762900000,
    "event": {
        "class": "OCA\\Deck\\Event\\CardCreatedEvent",
        "card": {
            "id": 4242,
            "title": "Webhook smoke test",
            "stackId": 16,
        },
    },
}


_DECK_CARD_DELETED = {
    "user": {"uid": "alice"},
    "time": 1762900100,
    "event": {
        "class": "OCA\\Deck\\Event\\CardDeletedEvent",
        "card": {
            "id": 4242,
            "title": "Webhook smoke test",
            "stackId": 16,
        },
    },
}


_DECK_BOARD_UPDATED = {
    "user": {"uid": "admin"},
    "time": 1762900200,
    "event": {
        "class": "OCA\\Deck\\Event\\BoardUpdatedEvent",
        "boardId": 5,
    },
}


_NOTE_CREATED_MISSING_ID = {
    "user": {"uid": "admin"},
    "time": 1762850300,
    "event": {
        "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        "node": {"path": "/admin/files/Notes/no-id.md"},
    },
}


_DECK_CARD_CREATED_MISSING_ID = {
    "user": {"uid": "admin"},
    "time": 1762900300,
    "event": {
        "class": "OCA\\Deck\\Event\\CardCreatedEvent",
        "card": {"title": "Card without id", "stackId": 16},
    },
}


def test_index_event_queues_task_and_returns_200():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["operation"] == "index"
    assert response.json()["doc_id"] == "437"

    task = receive_stream.receive_nowait()
    assert task.user_id == "admin"
    assert task.doc_id == "437"
    assert task.operation == "index"
    assert task.doc_type == "note"


def test_delete_event_queues_delete_task():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_DELETED)

    assert response.status_code == 200
    assert response.json()["operation"] == "delete"

    task = receive_stream.receive_nowait()
    assert task.operation == "delete"
    assert task.doc_id == "99"
    assert task.user_id == "alice"


def test_unsupported_event_is_ignored():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    payload = {
        "user": {"uid": "admin"},
        "time": 1,
        "event": {
            "class": "OCP\\Calendar\\Events\\CalendarObjectCreatedEvent",
            "objectData": {"id": 7},
        },
    }

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

    with pytest.raises(anyio.WouldBlock):
        receive_stream.receive_nowait()


def test_deck_card_created_queues_index_task():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_DECK_CARD_CREATED)

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["operation"] == "index"
    assert response.json()["doc_id"] == "4242"

    task = receive_stream.receive_nowait()
    assert task.user_id == "admin"
    assert task.doc_id == "4242"
    assert task.doc_type == "deck_card"
    assert task.operation == "index"
    assert task.metadata == {"stack_id": 16}


def test_deck_card_deleted_queues_delete_task():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_DECK_CARD_DELETED)

    assert response.status_code == 200
    assert response.json()["operation"] == "delete"

    task = receive_stream.receive_nowait()
    assert task.doc_type == "deck_card"
    assert task.operation == "delete"
    assert task.doc_id == "4242"
    assert task.user_id == "alice"


def test_deck_board_updated_is_ignored():
    """BoardUpdatedEvent carries no card id, so the parser logs delivery and
    returns None — the polling scanner picks up the actual card changes."""
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_DECK_BOARD_UPDATED)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

    with pytest.raises(anyio.WouldBlock):
        receive_stream.receive_nowait()


def test_deck_card_missing_id_is_ignored():
    """A card event without ``card.id`` can't address Qdrant points, so the
    parser logs a warning and returns None — the polling scanner reconciles."""
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/nextcloud", json=_DECK_CARD_CREATED_MISSING_ID
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

    with pytest.raises(anyio.WouldBlock):
        receive_stream.receive_nowait()


def test_note_missing_node_id_is_ignored():
    """Symmetric coverage for the file-event fail-open branch: a notes path
    without ``node.id`` falls back to the polling scanner."""
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED_MISSING_ID)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

    with pytest.raises(anyio.WouldBlock):
        receive_stream.receive_nowait()


def test_invalid_json_returns_400():
    app = _make_app(send_stream=None)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/nextcloud",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["status"] == "error"


def test_returns_503_when_send_stream_not_wired():
    """Vector sync not running → tell NC to retry instead of dropping the
    event."""
    app = _make_app(send_stream=None)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_returns_500_when_stream_is_closed():
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=1)
    receive_stream.close()  # close receiver → send raises BrokenResourceError
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 500
    assert response.json()["status"] == "error"


def test_returns_503_when_queue_is_full(monkeypatch):
    """When the processor queue is saturated, the handler must time out
    quickly with 503 instead of pinning until NC's outbound timeout fires."""
    # Speed up the test — a 1s deadline matches production but is overkill
    # for a unit test that's specifically exercising the timeout branch.
    # Capture the original BEFORE patching so the override doesn't recurse
    # into itself (the receiver imports the same anyio module object).
    real_fail_after = anyio.fail_after
    monkeypatch.setattr(
        "nextcloud_mcp_server.vector.webhook_receiver.anyio.fail_after",
        lambda _seconds: real_fail_after(0.05),
    )

    # Buffer of 1, no consumer → first send fills it, second blocks.
    send_stream, _receive_stream = anyio.create_memory_object_stream(max_buffer_size=1)
    send_stream.send_nowait("sentinel")  # type: ignore[arg-type]
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "queue full"


# --- WEBHOOK_SECRET authentication ---------------------------------------


def test_secret_set_valid_bearer_header_queues_task(monkeypatch):
    _patch_secret(monkeypatch, "supersecret")
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/nextcloud",
            json=_NOTE_CREATED,
            headers={"Authorization": "Bearer supersecret"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert receive_stream.receive_nowait().doc_id == "437"


def test_secret_set_missing_authorization_returns_401(monkeypatch):
    _patch_secret(monkeypatch, "supersecret")
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 401
    assert response.json()["status"] == "unauthorized"
    with pytest.raises(anyio.WouldBlock):
        receive_stream.receive_nowait()


def test_secret_set_wrong_secret_returns_401(monkeypatch):
    _patch_secret(monkeypatch, "supersecret")
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/nextcloud",
            json=_NOTE_CREATED,
            headers={"Authorization": "Bearer wrong"},
        )

    assert response.status_code == 401
    with pytest.raises(anyio.WouldBlock):
        receive_stream.receive_nowait()


def test_secret_set_wrong_scheme_returns_401(monkeypatch):
    """A token without the Bearer prefix is rejected."""
    _patch_secret(monkeypatch, "supersecret")
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/nextcloud",
            json=_NOTE_CREATED,
            headers={"Authorization": "supersecret"},
        )

    assert response.status_code == 401


def test_secret_unset_accepts_unauthenticated(monkeypatch):
    """Backward compat: deployments that haven't yet set WEBHOOK_SECRET keep
    working — the receiver accepts unauthenticated POSTs and logs a one-time
    warning."""
    _patch_secret(monkeypatch, None)
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post("/webhooks/nextcloud", json=_NOTE_CREATED)

    assert response.status_code == 200
    assert receive_stream.receive_nowait().doc_id == "437"


def test_compare_digest_is_called_with_bytes(monkeypatch, mocker):
    """Regression: secret comparison must run on bytes, not strings, so
    that future non-ASCII secret support doesn't depend on Python's
    implicit ASCII encoding."""
    _patch_secret(monkeypatch, "supersecret")
    spy = mocker.spy(webhook_receiver.hmac, "compare_digest")

    send_stream, _receive = anyio.create_memory_object_stream(max_buffer_size=4)
    app = _make_app(send_stream=send_stream)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/nextcloud",
            json=_NOTE_CREATED,
            headers={"Authorization": "Bearer supersecret"},
        )

    assert response.status_code == 200
    assert spy.call_count == 1
    provided_arg, expected_arg = spy.call_args.args
    assert isinstance(provided_arg, bytes)
    assert isinstance(expected_arg, bytes)
    assert expected_arg == b"Bearer supersecret"
