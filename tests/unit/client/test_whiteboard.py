"""Unit tests for reading Whiteboard board files.

A board is a file that any app or user with write access can fill, so the
parser is tested as a parser of untrusted input. The rest pins how the compact
view reads each feature of the format.
"""

import json

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st
from nextcloud_client.models.whiteboard import (
    MAX_GROUPS,
    MAX_ID_LENGTH,
    MAX_LINK_LENGTH,
    MAX_TEXT_LENGTH,
    InvalidBoardError,
    WhiteboardElement,
    parse_board,
    summarize_board,
)

pytestmark = pytest.mark.unit

#: Any JSON value. Floats include inf/nan: ``json.loads`` accepts ``Infinity``.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3)
    ),
    max_leaves=5,
)

#: Objects with the keys the summary reads, each holding an arbitrary value or
#: a plausible one, so that generation reaches past the first type check.
element_like = st.fixed_dictionaries(
    {
        "id": st.sampled_from(["a", "b", "\ud800"]) | json_values,
        "type": st.sampled_from(["text", "rectangle", "frame"]) | json_values,
    },
    optional={
        key: json_values
        for key in ("x", "y", "width", "height", "link", "frameId", "isDeleted")
    }
    | {
        "text": st.text() | json_values,
        "name": json_values,
        "originalText": json_values,
        "containerId": st.sampled_from(["a", "b"]) | json_values,
        "groupIds": st.lists(json_values, max_size=2) | json_values,
        "startBinding": st.fixed_dictionaries({"elementId": json_values}) | json_values,
        "endBinding": json_values,
        "customData": json_values
        | st.fixed_dictionaries(
            {"isTable": st.booleans(), "tableHtml": st.text() | json_values}
        )
        | st.fixed_dictionaries(
            {
                "type": st.just("comment"),
                "commentThread": json_values
                | st.fixed_dictionaries(
                    {
                        "comments": st.lists(
                            st.fixed_dictionaries(
                                {"text": json_values, "author": json_values}
                            )
                            | json_values,
                            max_size=2,
                        )
                        | json_values
                    }
                ),
            }
        ),
    },
)


def _element(element_id: str, element_type: str, **fields) -> dict:
    return {"id": element_id, "type": element_type, "isDeleted": False, **fields}


def _summary(*elements: dict) -> dict[str, dict]:
    """Serialized summary of a board of ``elements``, by element id."""
    content = json.dumps({"elements": elements, "files": {}}).encode()
    return {e.id: e.model_dump() for e in summarize_board(parse_board(content))}


# ── Parsing ─────────────────────────────────────────────────────────────


@given(content=st.binary())
@example(content=b"[" * 100_000)
@example(content=b'{"elements": [' + b"1" * 5000 + b"]}")
@example(content=b'{"elements": "\xff"}')
def test_parse_board_raises_only_invalid_board(content: bytes):
    """Any bytes either parse to a scene or are refused."""
    try:
        scene = parse_board(content)
    except InvalidBoardError:
        return
    assert isinstance(scene["elements"], list)


@given(value=json_values)
def test_json_without_an_element_list_is_not_a_board(value):
    """An unrelated JSON file must not read as an empty board."""
    for candidate in (value, value.get("data") if isinstance(value, dict) else None):
        assume(not isinstance(candidate, dict) or "elements" not in candidate)
    content = json.dumps(value).encode()
    with pytest.raises(InvalidBoardError):
        parse_board(content)


@pytest.mark.parametrize("content", [b"", b" \r\n\t"])
def test_new_board_is_an_empty_file(content: bytes):
    """Whiteboard creates a board as an empty file."""
    assert summarize_board(parse_board(content)) == []


def test_scene_may_be_wrapped_in_data():
    """Older files wrap the scene; the wrapper is removed."""
    scene = {"elements": [_element("a", "rectangle")], "unknown": {"kept": 1}}
    assert parse_board(json.dumps({"data": scene}).encode()) == scene


def test_unknown_keys_are_kept():
    """Keys of a newer app version come through unchanged."""
    scene = {"type": "excalidraw", "version": 2, "elements": [], "later": [1]}
    assert parse_board(json.dumps(scene).encode()) == scene


# ── Summary ─────────────────────────────────────────────────────────────


@given(elements=st.lists(element_like | json_values, max_size=5))
def test_summary_never_raises_and_always_serializes(elements: list):
    """A malformed field never fails the board or the response."""
    for element in summarize_board({"elements": elements}):
        assert element.id
        assert element.type
        element.model_dump_json().encode()


@given(elements=st.lists(element_like, max_size=5))
def test_absent_fields_are_not_serialized(elements: list):
    """No nulls in the output, and it still validates."""
    for element in summarize_board({"elements": elements}):
        dumped = element.model_dump()
        assert None not in dumped.values()
        assert WhiteboardElement.model_validate(dumped) == element


def test_styling_and_images_are_left_out():
    """Only id, type and rounded geometry are left of a bare image."""
    board = _summary(
        _element(
            "a",
            "image",
            x=1.4,
            y=-2.6,
            width=10,
            height=20.5001,
            seed=1,
            version=4,
            strokeColor="#1e1e1e",
            fileId="f",
        )
    )
    assert board == {
        "a": {
            "id": "a",
            "type": "image",
            "x": 1,
            "y": -3,
            "width": 10,
            "height": 21,
        }
    }


def test_deleted_elements_are_dropped():
    """The app keeps deleted elements in the file as tombstones."""
    assert _summary(_element("a", "rectangle") | {"isDeleted": True}) == {}


def test_label_becomes_the_text_of_its_container():
    """The label is folded in, without its wrapping line breaks."""
    board = _summary(
        _element("box", "rectangle"),
        _element(
            "label",
            "text",
            text="Two\nlines",
            originalText="Two lines",
            containerId="box",
        ),
    )
    assert board == {"box": {"id": "box", "type": "rectangle", "text": "Two lines"}}


def test_label_of_a_deleted_container_stands_alone():
    """A text whose container is gone is still shown."""
    board = _summary(
        _element("box", "rectangle") | {"isDeleted": True},
        _element("label", "text", text="Left", containerId="box"),
    )
    assert board == {"label": {"id": "label", "type": "text", "text": "Left"}}


def test_texts_that_contain_each_other_do_not_disappear():
    """A text is never a container, so a cycle hides nothing."""
    board = _summary(
        _element("a", "text", text="A", containerId="b"),
        _element("b", "text", text="B", containerId="a"),
    )
    assert {e["text"] for e in board.values()} == {"A", "B"}


def test_arrow_names_what_it_connects():
    """An attached end has the id; a free end has none."""
    board = _summary(
        _element("box", "rectangle"),
        _element(
            "arrow",
            "arrow",
            startBinding={"elementId": "box", "focus": 0, "gap": 4},
            endBinding=None,
        ),
        _element("label", "text", text="yes", containerId="arrow"),
    )
    assert board["arrow"] == {
        "id": "arrow",
        "type": "arrow",
        "text": "yes",
        "start": "box",
    }


def test_frame_group_and_link():
    """A frame's name is its text; members name their frame and groups."""
    board = _summary(
        _element("frame", "frame", name="Sprint 1"),
        _element(
            "box",
            "diamond",
            frameId="frame",
            groupIds=["inner", "outer"],
            link="https://example.com",
        ),
    )
    assert board["frame"]["text"] == "Sprint 1"
    assert board["box"] == {
        "id": "box",
        "type": "diamond",
        "frame": "frame",
        "groups": ["inner", "outer"],
        "link": "https://example.com",
    }


def test_table_reads_as_its_cells():
    """A table is an image with its HTML in customData."""
    html = (
        "<table><thead><tr><th>Name</th><th>Due</th></tr></thead>"
        "<tbody><tr><td><p>Ship</p><p>it</p></td><td>1 &amp; 2</td></tr></tbody></table>"
    )
    board = _summary(
        _element("t", "image", customData={"isTable": True, "tableHtml": html})
    )
    assert board["t"] == {
        "id": "t",
        "type": "table",
        "text": "Name | Due\nShip it | 1 & 2",
    }


def test_comment_reads_as_its_thread():
    """A comment is an invisible ellipse with its thread in customData."""
    thread = {
        "id": "1",
        "comments": [
            {"author": "Alice", "text": "Why?"},
            {"author": "Bob", "text": "Because."},
        ],
    }
    board = _summary(
        _element(
            "c", "ellipse", customData={"type": "comment", "commentThread": thread}
        )
    )
    assert board["c"] == {
        "id": "c",
        "type": "comment",
        "text": "Alice: Why?\nBob: Because.",
    }


# ── Bounds ──────────────────────────────────────────────────────────────


@given(excess=st.integers(min_value=-2, max_value=2))
def test_long_text_is_cut_off_and_says_so(excess: int):
    """Text over the limit is cut, and only then marked."""
    text = "x" * (MAX_TEXT_LENGTH + excess)
    element = _summary(_element("a", "text", text=text))["a"]
    assert element["text"] == text[:MAX_TEXT_LENGTH]
    assert element.get("text_truncated", False) is (excess > 0)


def test_labels_and_text_share_the_limit():
    """The cut applies to the joined text, not to each label."""
    half = "x" * (MAX_TEXT_LENGTH // 2)
    board = _summary(
        _element("box", "rectangle"),
        *(_element(f"l{i}", "text", text=half, containerId="box") for i in range(3)),
    )
    assert len(board["box"]["text"]) == MAX_TEXT_LENGTH
    assert board["box"]["text_truncated"] is True


def test_overlong_link_and_references_are_dropped():
    """Cutting a link or an id would point somewhere else."""
    long_id = "x" * (MAX_ID_LENGTH + 1)
    board = _summary(
        _element(
            "a",
            "arrow",
            link="x" * (MAX_LINK_LENGTH + 1),
            frameId=long_id,
            startBinding={"elementId": long_id},
            groupIds=[long_id, *(f"g{i}" for i in range(MAX_GROUPS + 5))],
        ),
        _element(long_id, "rectangle"),
    )
    assert board.keys() == {"a"}
    assert board["a"].keys() == {"id", "type", "groups"}
    assert len(board["a"]["groups"]) < MAX_GROUPS + 1


@given(elements=st.lists(element_like, max_size=3), size=st.integers(0, 200_000))
def test_one_element_is_bounded(elements: list, size: int):
    """However large its strings, one element stays well below a page."""
    for element in elements:
        for key in ("text", "originalText", "name", "link", "frameId"):
            element[key] = "x" * size
    for element in summarize_board({"elements": elements}):
        assert len(element.model_dump_json()) < 40_000
