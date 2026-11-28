"""Unit tests for the Nextcloud webhook payload parser.

Payload examples are taken from real Nextcloud captures recorded in
``webhook-testing-findings.md``.
"""

import string

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.vector.webhook_parser import (
    _indexable_suffixes,
    extract_document_task,
)
from tests.strategies import json_values


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
def test_markdown_outside_notes_is_left_to_the_scanner():
    """Only the Notes folder holds notes. Elsewhere a .md is a text file, which
    the polling scanner indexes when tagged: editors save .md files too often
    to queue a discovery per write."""
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


_EVENT_CLASSES = [
    "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
    "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
    "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent",
    "OCA\\Deck\\Event\\CardCreatedEvent",
    "OCA\\Deck\\Event\\CardUpdatedEvent",
    "OCA\\Deck\\Event\\CardDeletedEvent",
    "OCA\\Deck\\Event\\BoardUpdatedEvent",
    "OCP\\SystemTag\\MapperEvent",
]

_ids = st.integers() | st.text() | json_values
_paths = (
    st.from_regex(
        r"/[a-z]{1,5}/files/(Notes/)?[A-Za-z]{1,8}\.(md|pdf|txt)", fullmatch=True
    )
    | json_values
)


@st.composite
def webhook_payloads(draw):
    """Payloads shaped like Nextcloud's, with any field missing or wrong-typed:
    the known event classes and plausible paths keep every parser branch hot."""
    body = draw(
        st.fixed_dictionaries(
            {"class": st.sampled_from(_EVENT_CLASSES) | json_values},
            optional={
                "node": st.fixed_dictionaries({}, optional={"id": _ids, "path": _paths})
                | json_values,
                "card": st.fixed_dictionaries(
                    {}, optional={"id": _ids, "stackId": _ids}
                )
                | json_values,
                "objectType": st.just("files") | json_values,
                "objectId": _ids,
                "boardId": _ids,
            },
        )
    )
    return draw(
        st.fixed_dictionaries(
            {},
            optional={
                "event": st.just(body) | json_values,
                "user": st.fixed_dictionaries(
                    {}, optional={"uid": st.text() | json_values}
                )
                | json_values,
                "time": st.integers() | st.floats() | json_values,
            },
        )
    )


_NOTE_CREATED = {
    "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
    "node": {"id": 1, "path": "/admin/files/Notes/foo.md"},
}


@pytest.mark.unit
@given(webhook_payloads())
@example({})
@example({"time": 1, "event": _NOTE_CREATED})  # no user
@example({"user": {"uid": "admin"}, "time": "not-a-number", "event": _NOTE_CREATED})
@example({"user": {"uid": "admin"}, "time": float("inf"), "event": _NOTE_CREATED})
@example({"user": {"uid": "a"}, "event": {**_NOTE_CREATED, "node": [None]}})
def test_any_payload_yields_a_faithful_task_or_none(payload):
    """Total: a malformed payload is ignored, never an exception. A task is only
    ever built from a well-formed envelope, and carries exactly its values.

    Nearly every draw here yields ``None``; the ``return`` ends that (asserted:
    no exception) branch. The task branch is exercised by
    ``test_a_well_formed_note_event_yields_its_task``."""
    task = extract_document_task(payload)

    if task is None:
        return
    assert task.user_id == payload["user"]["uid"]
    assert isinstance(task.user_id, str) and task.user_id
    assert task.modified_at == int(payload.get("time", 0) or 0)
    assert isinstance(task.doc_id, str)
    assert task.operation in {"index", "delete"}


_FILE_EVENT_CLASSES = [c for c in _EVENT_CLASSES if c.startswith("OCP\\Files\\")]


@st.composite
def note_payloads(draw):
    """A well-formed file event for a note, and the task it must yield.

    ``webhook_payloads`` makes every key optional and wrong-typed, so it almost
    never draws a valid envelope: it proves totality, not faithfulness. This
    strategy only draws the valid shape, so every example reaches the task branch.
    """
    uid = draw(st.from_regex(r"[a-z]{1,8}", fullmatch=True))
    event_class = draw(st.sampled_from(_FILE_EVENT_CLASSES))
    node_id = draw(st.integers() | st.text(min_size=1))
    time = draw(st.integers(min_value=0, max_value=2**40))
    name = draw(st.from_regex(r"[A-Za-z0-9 ]{1,12}", fullmatch=True))
    payload = {
        "user": {"uid": uid},
        "time": time,
        "event": {
            "class": event_class,
            "node": {"id": node_id, "path": f"/{uid}/files/Notes/{name}.md"},
        },
    }
    operation = "delete" if event_class.endswith("BeforeNodeDeletedEvent") else "index"
    return payload, (uid, str(node_id), operation, time)


@pytest.mark.unit
@given(note_payloads())
def test_a_well_formed_note_event_yields_its_task(case):
    """A file event on a note always yields a task carrying the payload's
    user, node id and time, with the operation its event class implies."""
    payload, (uid, doc_id, operation, time) = case

    task = extract_document_task(payload)

    assert task is not None
    assert (task.user_id, task.doc_id, task.doc_type, task.operation) == (
        uid,
        doc_id,
        "note",
        operation,
    )
    assert task.modified_at == time


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


# --- Regular files (doc_type="file") ---------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "event_class",
    [
        "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
    ],
)
def test_indexable_file_event_returns_reconcile_task(event_class):
    """A PDF write can't say whether the file is tagged, so it goes out as a
    reconcile task (``file_path is None``) for the processor to resolve."""
    payload = {
        "user": {"uid": "alice"},
        "time": 1762850245,
        "event": {
            "class": event_class,
            "node": {"id": 8123, "path": "/alice/files/Documents/report.pdf"},
        },
    }

    task = extract_document_task(payload)

    assert task is not None
    assert task.doc_type == "file"
    assert task.doc_id == "8123"
    assert task.operation == "index"
    assert task.file_path is None
    assert task.modified_at == 1762850245


@pytest.mark.unit
def test_indexable_file_delete_returns_delete_task():
    payload = {
        "user": {"uid": "alice"},
        "time": 1762850245,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent",
            "node": {"id": 8123, "path": "/alice/files/Documents/report.pdf"},
        },
    }

    task = extract_document_task(payload)

    assert task is not None
    assert task.doc_type == "file"
    assert task.operation == "delete"
    assert task.doc_id == "8123"


@pytest.mark.unit
def test_indexable_file_suffix_is_case_insensitive():
    payload = {
        "user": {"uid": "alice"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 9, "path": "/alice/files/Scans/SCAN.PDF"},
        },
    }

    task = extract_document_task(payload)

    assert task is not None
    assert task.doc_type == "file"


@pytest.mark.unit
def test_non_indexable_file_event_returns_none():
    """Types outside VECTOR_SYNC_INDEXABLE_MIME_TYPES stay ignored — no
    reconcile job for every image saved on the instance."""
    payload = {
        "user": {"uid": "alice"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
            "node": {"id": 10, "path": "/alice/files/Photos/holiday.jpg"},
        },
    }

    assert extract_document_task(payload) is None


def _file_written(path: str) -> dict:
    return {
        "user": {"uid": "alice"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
            "node": {"id": 12, "path": path},
        },
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["contract.docx", "sheet.xlsx", "deck.pptx", "mail.msg"],
)
def test_readable_files_are_indexable_by_default(name):
    task = extract_document_task(_file_written(f"/alice/files/Docs/{name}"))

    assert task is not None
    assert task.doc_type == "file"


@pytest.mark.unit
def test_webhook_suffixes_follow_the_indexable_mime_setting(monkeypatch):
    """Narrowing VECTOR_SYNC_INDEXABLE_MIME_TYPES narrows the webhook too, so
    the push path never enqueues a type the scanner would not discover."""
    from nextcloud_mcp_server import config  # noqa: PLC0415

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(vector_sync_indexable_mime_types="application/pdf"),
    )

    assert extract_document_task(_file_written("/alice/files/Docs/a.docx")) is None
    assert extract_document_task(_file_written("/alice/files/Docs/a.pdf")) is not None


# Texts rather than ``st.from_regex``, and the folder depth drawn first rather
# than ``st.lists``: both make the number of draws vary with the value, so
# Hypothesis's mutations often outgrew the example and discarded it.
#: ``[A-Za-z0-9 _-]{1,10}``
_segments = st.text(
    string.ascii_letters + string.digits + " _-", min_size=1, max_size=10
)
#: ``[a-z]{1,8}``
_uids = st.text(string.ascii_lowercase, min_size=1, max_size=8)


@st.composite
def _folders(draw) -> str:
    """Folders under a user's files root, ``Notes`` included: a non-markdown
    file there is an ordinary file, not a note. Up to three deep."""
    depth = draw(st.integers(0, 3))
    return "/".join(draw(_segments | st.just("Notes")) for _ in range(depth))


def _file_path(uid: str, folder: str, name: str, suffix: str) -> str:
    return "/".join(p for p in (f"/{uid}/files", folder, f"{name}{suffix}") if p)


@st.composite
def indexable_file_payloads(draw):
    """A well-formed file event for an indexable, non-note file, and the
    reconcile task it must yield. The suffix comes from the indexable MIME
    setting (default config), in any letter case."""
    suffixes = _indexable_suffixes()
    assert suffixes, "the default config indexes at least one file type"
    uid = draw(_uids)
    event_class = draw(st.sampled_from(_FILE_EVENT_CLASSES))
    node_id = draw(st.integers() | st.text(min_size=1))
    time = draw(st.integers(min_value=0, max_value=2**40))
    recase = draw(st.sampled_from([str.lower, str.upper, str.title]))
    suffix = recase(draw(st.sampled_from(suffixes)))
    path = _file_path(uid, draw(_folders()), draw(_segments), suffix)
    payload = {
        "user": {"uid": uid},
        "time": time,
        "event": {"class": event_class, "node": {"id": node_id, "path": path}},
    }
    operation = "delete" if event_class.endswith("BeforeNodeDeletedEvent") else "index"
    return payload, (uid, str(node_id), operation, time)


@pytest.mark.unit
@given(indexable_file_payloads())
def test_an_indexable_file_event_yields_a_reconcile_task(case):
    """Any indexable suffix (case-insensitive), in any folder (Notes/
    included), yields a file task with ``file_path=None``, so the processor
    resolves tag membership."""
    payload, (uid, doc_id, operation, time) = case

    task = extract_document_task(payload)

    assert task is not None
    assert (task.user_id, task.doc_id, task.doc_type, task.operation) == (
        uid,
        doc_id,
        "file",
        operation,
    )
    assert task.modified_at == time
    assert task.file_path is None


#: Suffixes vector sync does not index. ``.md`` is excluded because under
#: ``Notes/`` it is a note.
_ignored_suffixes = (
    st.text(string.ascii_letters + string.digits, min_size=1, max_size=5)
    .map(".{}".format)
    .filter(lambda s: s.lower() not in _indexable_suffixes() and s.lower() != ".md")
)


@pytest.mark.unit
@given(
    event_class=st.sampled_from(_FILE_EVENT_CLASSES),
    folder=_folders(),
    name=_segments,
    suffix=_ignored_suffixes,
)
def test_a_non_indexable_file_event_is_ignored(event_class, folder, name, suffix):
    """No reconcile job for every image or archive saved on the instance."""
    payload = {
        "user": {"uid": "alice"},
        "time": 1,
        "event": {
            "class": event_class,
            "node": {"id": 1, "path": _file_path("alice", folder, name, suffix)},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_indexable_file_without_node_id_returns_none():
    payload = {
        "user": {"uid": "alice"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent",
            "node": {"path": "/alice/files/Documents/report.pdf"},
        },
    }

    assert extract_document_task(payload) is None


@pytest.mark.unit
def test_pdf_inside_notes_folder_is_a_file_not_a_note():
    """The Notes branch only claims markdown; a PDF stored under Notes/ is an
    ordinary tagged-file candidate."""
    payload = {
        "user": {"uid": "alice"},
        "time": 1,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": 11, "path": "/alice/files/Notes/attachment.pdf"},
        },
    }

    task = extract_document_task(payload)

    assert task is not None
    assert task.doc_type == "file"
