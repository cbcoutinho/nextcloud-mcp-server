"""Unit tests for the Nextcloud webhook payload parser.

Payload examples are taken from real Nextcloud captures recorded in
``webhook-testing-findings.md``.
"""

import pytest

from nextcloud_mcp_server.vector.webhook_parser import extract_document_task


@pytest.mark.unit
def test_node_created_event_returns_index_task():
    payload = {
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

    task = extract_document_task(payload)

    assert task is not None
    assert task.user_id == "admin"
    assert task.doc_id == "437"
    assert task.doc_type == "note"
    assert task.operation == "index"
    assert task.modified_at == 1762850245


@pytest.mark.unit
def test_node_written_event_returns_index_task():
    payload = {
        "user": {"uid": "admin", "displayName": "admin"},
        "time": 1762850960,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
            "node": {
                "id": 437,
                "path": "/admin/files/Notes/Webhooks/Webhook Test Note.md",
            },
        },
    }

    task = extract_document_task(payload)

    assert task is not None
    assert task.operation == "index"
    assert task.doc_id == "437"


@pytest.mark.unit
def test_before_node_deleted_event_returns_delete_task():
    payload = {
        "user": {"uid": "alice", "displayName": "Alice"},
        "time": 1762851093,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent",
            "node": {
                "id": 437,
                "path": "/alice/files/Notes/Webhooks/Webhook Test Note.md",
            },
        },
    }

    task = extract_document_task(payload)

    assert task is not None
    assert task.user_id == "alice"
    assert task.operation == "delete"
    assert task.doc_id == "437"
    assert task.doc_type == "note"


@pytest.mark.unit
def test_node_id_is_normalized_to_string():
    """NC sends node.id as int; we always emit str (per ADR-010 §A.3)."""
    payload = {
        "user": {"uid": "admin"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 437, "path": "/admin/files/Notes/foo.md"},
        },
    }

    task = extract_document_task(payload)

    assert task is not None
    assert isinstance(task.doc_id, str)
    assert task.doc_id == "437"


@pytest.mark.unit
def test_path_outside_notes_returns_none():
    payload = {
        "user": {"uid": "admin"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 1, "path": "/admin/files/Documents/foo.md"},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_non_markdown_inside_notes_returns_none():
    payload = {
        "user": {"uid": "admin"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 1, "path": "/admin/files/Notes/image.png"},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_parent_folder_event_returns_none():
    """Creating a note fires events for the parent folder too — ignore those."""
    payload = {
        "user": {"uid": "admin"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 100, "path": "/admin/files/Notes/Webhooks"},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_unknown_event_class_returns_none():
    payload = {
        "user": {"uid": "admin"},
        "time": 1,
        "event": {
            "class": "OCP\\Calendar\\Events\\CalendarObjectCreatedEvent",
            "objectData": {"id": 7, "uri": "x.ics"},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_node_deleted_event_without_id_returns_none():
    """``NodeDeletedEvent`` (no node.id) is not the registered event, but if
    one ever leaks through we ignore it rather than guess at the doc_id —
    the polling scanner will catch up via its grace period."""
    payload = {
        "user": {"uid": "admin"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent",
            "node": {"path": "/admin/files/Notes/foo.md"},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_missing_user_field_returns_none():
    payload = {
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 1, "path": "/admin/files/Notes/foo.md"},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_empty_payload_returns_none():
    assert extract_document_task({}) is None


@pytest.mark.unit
def test_non_numeric_time_field_returns_none():
    """A malformed ``time`` field must not raise ValueError out of the parser."""
    payload = {
        "user": {"uid": "admin"},
        "time": "not-a-number",
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 1, "path": "/admin/files/Notes/foo.md"},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_missing_time_field_defaults_to_zero():
    payload = {
        "user": {"uid": "admin"},
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 1, "path": "/admin/files/Notes/foo.md"},
        },
    }

    task = extract_document_task(payload)
    assert task is not None
    assert task.modified_at == 0


# --- SystemTag MapperEvent (assign/unassign) -------------------------------
#
# NC 32+ serializes the event as {eventType, objectType, objectId, tagIds}.
# Both directions collapse to a path-less file "reconcile" task; the processor
# resolves current vector-index membership to decide index-vs-delete.

_TAG_MAPPER = "OCP\\SystemTag\\MapperEvent"


@pytest.mark.unit
def test_tag_assign_event_returns_reconcile_task():
    payload = {
        "user": {"uid": "alice"},
        "time": 1762850245,
        "event": {
            "class": _TAG_MAPPER,
            "eventType": "OCP\\SystemTag\\ISystemTagObjectMapper::assignTags",
            "objectType": "files",
            "objectId": "478087",
            "tagIds": [7],
        },
    }

    task = extract_document_task(payload)
    assert task is not None
    assert task.user_id == "alice"
    assert task.doc_type == "file"
    assert task.doc_id == "478087"
    assert task.operation == "index"
    # No path in the payload -> the processor reconciles membership.
    assert task.file_path is None
    assert task.modified_at == 1762850245


@pytest.mark.unit
def test_tag_unassign_event_also_returns_reconcile_task():
    """Unassign collapses to the same reconcile (processor flips to delete)."""
    payload = {
        "user": {"uid": "alice"},
        "time": 1762850245,
        "event": {
            "class": _TAG_MAPPER,
            "eventType": "OCP\\SystemTag\\ISystemTagObjectMapper::unassignTags",
            "objectType": "files",
            "objectId": "478087",
            "tagIds": [7],
        },
    }

    task = extract_document_task(payload)
    assert task is not None
    assert task.doc_type == "file"
    assert task.operation == "index"
    assert task.file_path is None


@pytest.mark.unit
def test_tag_event_non_files_object_type_returns_none():
    """Tags on non-file objects (e.g. comments) don't drive vector sync."""
    payload = {
        "user": {"uid": "alice"},
        "time": 1762850245,
        "event": {
            "class": _TAG_MAPPER,
            "eventType": "OCP\\SystemTag\\ISystemTagObjectMapper::assignTags",
            "objectType": "comments",
            "objectId": "12",
            "tagIds": [7],
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_tag_event_missing_object_id_returns_none():
    payload = {
        "user": {"uid": "alice"},
        "time": 1762850245,
        "event": {
            "class": _TAG_MAPPER,
            "eventType": "OCP\\SystemTag\\ISystemTagObjectMapper::assignTags",
            "objectType": "files",
            "objectId": "",
            "tagIds": [7],
        },
    }

    assert extract_document_task(payload) is None
