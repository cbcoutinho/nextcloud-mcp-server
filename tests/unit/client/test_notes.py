"""Unit tests for the NotesClient response-shape guard.

Notes app v5.0.0 has cases where the API returns a list-shaped JSON payload
where the MCP server expects a single note object — see issue #730. The
``_expect_note_object`` helper coerces these and surfaces clear errors instead
of letting Pydantic raise the cryptic ``"argument after ** must be a mapping,
not list"``.
"""

import pytest
from nextcloud_client.notes import _expect_note_object

pytestmark = pytest.mark.unit


def test_dict_payload_passes_through():
    """The healthy case: API returns a single note object — return it unchanged."""
    payload = {"id": 1, "title": "Test", "content": "body", "etag": "abc"}
    assert _expect_note_object(payload, operation="create_note") is payload


def test_single_element_list_is_unwrapped(caplog):
    """Notes v5.0.0 sometimes wraps a single note in a list; unwrap it and
    log a warning so operators notice the upstream quirk.
    """
    inner = {"id": 1, "title": "Test", "etag": "abc"}
    with caplog.at_level("WARNING"):
        result = _expect_note_object([inner], operation="create_note")
    assert result is inner
    assert any("single-element list" in r.message for r in caplog.records)


def test_empty_list_raises_clear_error():
    """notes_api#fail returns ``[]`` for unmatched routes (#730). Surface a
    diagnostic error rather than letting Pydantic complain about ``** mapping``.
    """
    with pytest.raises(ValueError) as exc:
        _expect_note_object([], operation="create_note")
    msg = str(exc.value)
    assert "create_note" in msg
    assert "list-shaped payload" in msg
    assert "notes_api#fail" in msg


def test_multi_element_list_raises_clear_error():
    """Defensive: a list with multiple elements is also wrong shape; bail loudly."""
    with pytest.raises(ValueError) as exc:
        _expect_note_object([{"id": 1}, {"id": 2}], operation="update_note")
    assert "update_note" in str(exc.value)


def test_non_dict_non_list_raises_clear_error():
    """Defensive: a string / int / None payload is also unexpected."""
    with pytest.raises(ValueError) as exc:
        _expect_note_object("not a note", operation="create_note")
    assert "unexpected payload type" in str(exc.value)
    assert "str" in str(exc.value)


def test_list_of_non_dict_raises_clear_error():
    """A list whose element isn't a dict still fails with a clear error."""
    with pytest.raises(ValueError) as exc:
        _expect_note_object(["not a note"], operation="get_note")
    assert "list-shaped payload" in str(exc.value)
