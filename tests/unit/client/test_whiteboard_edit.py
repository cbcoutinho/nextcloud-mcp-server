"""Unit tests for editing Whiteboard boards.

The properties pin the rules any edit must keep, whatever the board holds:
untouched elements stay identical, changed ones get a higher version, new ones
sort after the old ones, and only ``BoardEditError`` escapes. The examples pin
how each operation places labels and arrows.
"""

import copy
import json
import math

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from nextcloud_client.models.whiteboard import parse_board, summarize_board
from nextcloud_client.models.whiteboard_edit import (
    BoardEditError,
    ElementChange,
    NewElement,
    add_elements,
    delete_elements,
    measure_text,
    serialize_board,
    update_elements,
    wrap_text,
)

pytestmark = pytest.mark.unit

NOW = 1_800_000_000_000

#: Any JSON value. Floats include inf/nan: ``json.loads`` accepts ``Infinity``.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3)
    ),
    max_leaves=5,
)

ids = st.sampled_from(["a", "b", "c", "d", "frame"])
coordinates = st.floats(-500, 500, allow_nan=False)

#: Elements shaped like real ones, with arbitrary values mixed in, and keys
#: from a newer app that the edit must keep.
existing_elements = st.fixed_dictionaries(
    {
        "id": ids,
        "type": st.sampled_from(
            ["rectangle", "ellipse", "diamond", "text", "arrow", "frame", "image"]
        ),
        "x": coordinates | json_values,
        "y": coordinates,
        "width": st.floats(1, 300, allow_nan=False) | json_values,
        "height": st.floats(1, 300, allow_nan=False),
    },
    optional={
        "version": st.integers(-2, 50) | json_values,
        "isDeleted": st.booleans() | json_values,
        "index": st.sampled_from(["a0", "a1", "a5", "b00", "Zz", "b", "z", ""])
        | json_values,
        "containerId": ids | json_values,
        "boundElements": st.lists(
            st.fixed_dictionaries(
                {"id": ids, "type": st.sampled_from(["text", "arrow"])}
            ),
            max_size=3,
        )
        | json_values,
        "startBinding": st.fixed_dictionaries({"elementId": ids}) | json_values,
        "endBinding": st.fixed_dictionaries({"elementId": ids}) | json_values,
        "points": st.lists(
            st.tuples(coordinates, coordinates).map(list), min_size=0, max_size=4
        )
        | json_values,
        "frameId": ids | st.none(),
        "text": st.text(max_size=20) | json_values,
        "locked": st.booleans(),
        "customData": json_values,
        "futureKey": json_values,
    },
)

#: Elements as the app saves them, so that most generated edits succeed.
sane_elements = st.fixed_dictionaries(
    {
        "id": ids,
        "type": st.sampled_from(["rectangle", "ellipse", "diamond", "text", "frame"]),
        "x": coordinates,
        "y": coordinates,
        "width": st.floats(1, 300, allow_nan=False),
        "height": st.floats(1, 300, allow_nan=False),
        "version": st.integers(1, 50),
        "isDeleted": st.just(False),
        "index": st.sampled_from(["a0", "a1", "a5", "b00"]),
    },
    optional={"futureKey": json_values, "customData": json_values},
)


def _board(elements: list, junk: list) -> dict:
    return {"elements": [*elements, *junk], "appState": {"kept": True}}


#: Mostly sane boards with unique ids, some with odd elements, and some junk
#: entries in the element list.
scenes = st.builds(
    _board,
    st.lists(
        st.one_of(sane_elements, sane_elements, sane_elements, existing_elements),
        max_size=5,
        unique_by=lambda element: element["id"],
    ),
    st.lists(json_values, max_size=2),
)

labels = st.none() | st.text(max_size=60)
sizes = st.none() | st.floats(1, 400, allow_nan=False)

#: Additions that are valid on their own, so that most generated calls succeed.
#: Whether they fit the board (ids taken, missing ends) is left to chance.
new_elements = st.one_of(
    st.builds(
        NewElement,
        type=st.sampled_from(["rectangle", "ellipse", "diamond"]),
        id=st.none() | st.sampled_from(["n1", "n2"]),
        x=coordinates,
        y=coordinates,
        width=sizes,
        height=sizes,
        text=labels,
        font_size=st.none() | st.integers(8, 60),
    ),
    st.builds(
        NewElement,
        type=st.just("text"),
        x=coordinates,
        y=coordinates,
        text=st.text(min_size=1, max_size=60),
    ),
    st.builds(
        NewElement,
        type=st.just("arrow"),
        start=ids | st.just("n1"),
        end=ids | st.just("n2"),
        text=labels,
    ),
    st.builds(
        NewElement,
        type=st.just("frame"),
        x=coordinates,
        y=coordinates,
        width=st.floats(1, 400, allow_nan=False),
        height=st.floats(1, 400, allow_nan=False),
        text=labels,
    ),
    st.builds(
        NewElement,
        type=st.sampled_from(
            ["rectangle", "ellipse", "diamond", "text", "arrow", "frame"]
        ),
        id=st.none() | st.sampled_from(["n1", "n2", "a"]),
        x=st.none() | coordinates,
        y=st.none() | coordinates,
        width=sizes,
        height=sizes,
        text=labels,
        start=st.none() | ids,
        end=st.none() | ids,
        frame=st.none() | ids,
    ),
)


def _changes(element_ids: st.SearchStrategy[str]) -> st.SearchStrategy[ElementChange]:
    return st.builds(
        ElementChange,
        id=element_ids,
        text=st.none() | st.text(max_size=60),
        x=st.none() | coordinates,
        y=st.none() | coordinates,
        width=st.none() | st.floats(1, 400, allow_nan=False),
        height=st.none() | st.floats(1, 400, allow_nan=False),
        font_size=st.none() | st.integers(8, 60),
    )


@st.composite
def edits(draw: st.DrawFn) -> tuple[dict, tuple]:
    """A board, and an edit that mostly names elements the board has."""
    scene = draw(scenes)
    present = [e["id"] for e in scene["elements"] if isinstance(e, dict) and "id" in e]
    element_ids = st.sampled_from(present) | ids if present else ids
    operation = draw(
        st.one_of(
            st.lists(new_elements, min_size=1, max_size=3).map(lambda n: ("add", n)),
            st.lists(
                _changes(element_ids), min_size=1, max_size=3, unique_by=lambda c: c.id
            ).map(lambda c: ("update", c)),
            st.lists(element_ids, min_size=1, max_size=3).map(lambda i: ("delete", i)),
        )
    )
    return scene, operation


def _apply(scene: dict, operation: tuple) -> list[str]:
    kind, argument = operation
    if kind == "add":
        return add_elements(scene, argument, now_ms=NOW)
    if kind == "update":
        return update_elements(scene, argument, now_ms=NOW)
    return delete_elements(scene, argument, now_ms=NOW)


def _version(element: object) -> int:
    version = element.get("version") if isinstance(element, dict) else None
    ok = isinstance(version, int) and not isinstance(version, bool) and version > 0
    return version if ok else 0


# ── Properties ──────────────────────────────────────────────────────────


_BOX = {"type": "rectangle", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(edit=edits())
# A refused change must restore the board exactly, NaN included.
@example(
    edit=(
        {
            "elements": [
                {**_BOX, "id": "b", "version": 1, "isDeleted": False, "index": "a0"},
                {**_BOX, "id": "a", "width": [[math.nan]]},
            ],
            "appState": {},
        },
        ("update", [ElementChange(id="b", font_size=8)]),
    )
)
# A malformed index still sorts below the new element's.
@example(
    edit=(
        {"elements": [{**_BOX, "id": "a", "index": "b"}], "appState": {}},
        ("add", [NewElement(type="rectangle", x=0, y=0)]),
    )
)
def test_an_edit_keeps_the_rules(edit: tuple[dict, tuple]):
    """Untouched elements stay identical, changed ones get a higher version,
    nothing is removed, and new elements sort after the old ones."""
    scene, operation = edit
    before = copy.deepcopy(scene)
    try:
        _apply(scene, operation)
    except BoardEditError:
        assert scene == before, "a refused edit changed the board"
        return

    old, new = (
        scene["elements"][: len(before["elements"])],
        scene["elements"][len(before["elements"]) :],
    )
    assert scene["appState"] == before["appState"]
    for was, now in zip(before["elements"], old, strict=True):
        # Identity first: a NaN in the list is not equal to itself.
        if now is not was and now != was:
            assert isinstance(now, dict) and isinstance(was, dict)
            assert _version(now) > _version(was)
            assert now["updated"] == NOW
            # Keys of a newer app survive a change.
            assert {k: v for k, v in was.items() if k == "futureKey"} == {
                k: v for k, v in now.items() if k == "futureKey"
            }
    old_keys = [
        e["index"]
        for e in before["elements"]
        if isinstance(e, dict) and isinstance(e.get("index"), str)
    ]
    previous = max(old_keys) if old_keys else ""
    for element in new:
        assert element["version"] >= 1
        # Past a key starting with "z" or later, no valid key sorts higher.
        if not previous or previous[0] < "z":
            assert element["index"] > previous
        previous = element["index"]


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(edit=edits())
def test_an_edited_board_reads_back(edit: tuple[dict, tuple]):
    """What an edit saves is JSON the reader, and so the browser, can read."""
    scene, operation = edit
    try:
        _apply(scene, operation)
        content = serialize_board(scene)
    except BoardEditError:
        return
    summarize_board(parse_board(content))


# ── Adding ──────────────────────────────────────────────────────────────


def _scene(*elements: dict) -> dict:
    return {"elements": list(elements)}


def _by_id(scene: dict) -> dict[str, dict]:
    return {e["id"]: e for e in scene["elements"] if not e.get("isDeleted")}


def test_a_new_shape_is_complete():
    """Every field the app saves is there, so opening the board rewrites nothing."""
    scene = _scene()
    [element_id] = add_elements(
        scene, [NewElement(type="rectangle", x=10, y=20)], now_ms=NOW
    )
    element = _by_id(scene)[element_id]
    assert element["index"] == "a0"
    assert (element["width"], element["height"]) == (200, 100)
    for key in ("seed", "versionNonce", "strokeColor", "roughness", "roundness"):
        assert key in element
    assert element["updated"] == NOW
    assert len(element_id) == 21


def test_a_label_is_bound_and_centred():
    scene = _scene()
    [box_id] = add_elements(
        scene, [NewElement(type="rectangle", x=0, y=0, text="Hello")], now_ms=NOW
    )
    elements = _by_id(scene)
    box = elements[box_id]
    [label] = [e for e in elements.values() if e.get("containerId") == box_id]
    assert box["boundElements"] == [{"id": label["id"], "type": "text"}]
    assert label["text"] == label["originalText"] == "Hello"
    assert label["x"] + label["width"] / 2 == pytest.approx(100)
    assert label["y"] + label["height"] / 2 == pytest.approx(50)
    assert label["index"] > box["index"]


def test_a_long_label_wraps_and_the_shape_grows():
    scene = _scene()
    text = "a fairly long label that cannot fit on one line of a small box"
    [box_id] = add_elements(
        scene,
        [NewElement(type="rectangle", x=0, y=0, width=120, height=40, text=text)],
        now_ms=NOW,
    )
    box = _by_id(scene)[box_id]
    [label] = [e for e in scene["elements"] if e.get("containerId") == box_id]
    assert "\n" in label["text"] and label["originalText"] == text
    assert label["width"] <= 120 - 10
    assert box["height"] >= label["height"] + 10


def test_an_arrow_connects_elements_added_in_the_same_call():
    scene = _scene()
    added = add_elements(
        scene,
        [
            NewElement(type="rectangle", id="left", x=0, y=0),
            NewElement(type="ellipse", id="right", x=400, y=0),
            NewElement(type="arrow", id="link", start="left", end="right", text="yes"),
        ],
        now_ms=NOW,
    )
    assert added == ["left", "right", "link"]
    elements = _by_id(scene)
    arrow = elements["link"]
    assert arrow["startBinding"]["elementId"] == "left"
    assert arrow["endBinding"]["elementId"] == "right"
    assert {"id": "link", "type": "arrow"} in elements["left"]["boundElements"]
    assert {"id": "link", "type": "arrow"} in elements["right"]["boundElements"]
    # From the rectangle's right edge to the ellipse's left edge, with the gap.
    assert arrow["x"] == pytest.approx(204)
    assert arrow["y"] == pytest.approx(50)
    assert arrow["points"][-1] == [pytest.approx(396 - 204), pytest.approx(0)]
    view = {e.id: e for e in summarize_board(parse_board(serialize_board(scene)))}
    assert (view["link"].start, view["link"].end, view["link"].text) == (
        "left",
        "right",
        "yes",
    )


@pytest.mark.parametrize(
    ("new", "message"),
    [
        ([NewElement(type="arrow", start="a", end="nope")], "no element 'nope'"),
        ([NewElement(type="arrow", start="a", end="a")], "two different"),
        ([NewElement(type="arrow", start="a")], "start and end"),
        ([NewElement(type="rectangle", x=0)], "needs x and y"),
        ([NewElement(type="text", x=0, y=0)], "needs text"),
        ([NewElement(type="frame", x=0, y=0, width=10)], "width and height"),
        ([NewElement(type="rectangle", id="a", x=0, y=0)], "already has"),
        ([NewElement(type="rectangle", x=0, y=0, frame="a")], "not a frame"),
    ],
)
def test_an_impossible_addition_changes_nothing(new, message):
    """One bad element refuses the whole call."""
    scene = _scene(
        {"id": "a", "type": "rectangle", "x": 0, "y": 0, "width": 10, "height": 10}
    )
    before = copy.deepcopy(scene)
    with pytest.raises(BoardEditError, match=message):
        add_elements(scene, [NewElement(type="ellipse", x=0, y=0), *new], now_ms=NOW)
    assert scene == before


def test_an_element_outside_its_frame_is_refused():
    scene = _scene(
        {"id": "f", "type": "frame", "x": 0, "y": 0, "width": 100, "height": 100}
    )
    with pytest.raises(BoardEditError, match="inside frame"):
        add_elements(
            scene, [NewElement(type="rectangle", x=50, y=50, frame="f")], now_ms=NOW
        )


# ── Changing ────────────────────────────────────────────────────────────


def _two_boxes_and_an_arrow() -> dict:
    scene = _scene()
    add_elements(
        scene,
        [
            NewElement(type="rectangle", id="left", x=0, y=0, text="Left"),
            NewElement(type="rectangle", id="right", x=400, y=0),
            NewElement(type="arrow", id="link", start="left", end="right"),
        ],
        now_ms=NOW,
    )
    return scene


def test_moving_a_shape_moves_its_label_and_reroutes_its_arrows():
    scene = _two_boxes_and_an_arrow()
    label = next(e for e in scene["elements"] if e.get("containerId") == "left")
    label_x = label["x"]
    changed = update_elements(
        scene, [ElementChange(id="left", x=0, y=300)], now_ms=NOW + 1
    )
    assert set(changed) == {"left", label["id"], "link"}
    assert label["y"] > 300 and label["x"] == label_x
    arrow = _by_id(scene)["link"]
    end_x = arrow["x"] + arrow["points"][-1][0]
    end_y = arrow["y"] + arrow["points"][-1][1]
    # The arrow now runs up and to the right, from the moved box to the
    # bottom edge of the right box (y 0 to 100), plus the gap.
    assert arrow["x"] >= 100 and arrow["y"] <= 300
    assert 400 <= end_x <= 600
    assert 100 < end_y <= 104


def test_moving_a_frame_moves_what_is_in_it():
    scene = _scene()
    add_elements(
        scene,
        [
            NewElement(type="frame", id="f", x=0, y=0, width=500, height=300),
            NewElement(type="rectangle", id="in", x=10, y=10, frame="f", text="In"),
            NewElement(type="rectangle", id="out", x=900, y=0),
        ],
        now_ms=NOW,
    )
    update_elements(scene, [ElementChange(id="f", x=100)], now_ms=NOW + 1)
    elements = _by_id(scene)
    assert elements["in"]["x"] == 110
    assert elements["out"]["x"] == 900


def _framed() -> dict:
    scene = _scene()
    add_elements(
        scene,
        [
            NewElement(type="frame", id="f", x=0, y=0, width=500, height=300),
            NewElement(type="rectangle", id="in", x=10, y=10, frame="f", text="In"),
        ],
        now_ms=NOW,
    )
    return scene


def _label_of(scene: dict, container_id: str) -> dict:
    return next(e for e in scene["elements"] if e.get("containerId") == container_id)


def test_an_element_moved_out_of_its_frame_leaves_it():
    """As when dragged in the app. Otherwise the frame clips it out of sight."""
    scene = _framed()
    update_elements(scene, [ElementChange(id="in", x=900)], now_ms=NOW + 1)
    assert _by_id(scene)["in"]["frameId"] is None
    assert _label_of(scene, "in")["frameId"] is None


def test_an_element_resized_out_of_its_frame_leaves_it():
    scene = _framed()
    update_elements(scene, [ElementChange(id="in", width=800)], now_ms=NOW + 1)
    assert _by_id(scene)["in"]["frameId"] is None


def test_an_element_moved_within_its_frame_stays_in_it():
    scene = _framed()
    update_elements(scene, [ElementChange(id="in", x=200, y=150)], now_ms=NOW + 1)
    assert _by_id(scene)["in"]["frameId"] == "f"
    assert _label_of(scene, "in")["frameId"] == "f"


def test_a_bent_arrow_keeps_its_middle_points():
    """An arrow bent in the app: only its bound ends move to the moved element."""
    scene = _scene(
        {
            "id": "a",
            "type": "rectangle",
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 100,
            "boundElements": [{"id": "r", "type": "arrow"}],
        },
        {
            "id": "b",
            "type": "rectangle",
            "x": 400,
            "y": 0,
            "width": 100,
            "height": 100,
            "boundElements": [{"id": "r", "type": "arrow"}],
        },
        {
            "id": "r",
            "type": "arrow",
            "x": 104,
            "y": 50,
            "points": [[0, 0], [150, -120], [292, 0]],
            "startBinding": {"elementId": "a"},
            "endBinding": {"elementId": "b"},
        },
    )
    update_elements(scene, [ElementChange(id="a", y=200)], now_ms=NOW)
    arrow = _by_id(scene)["r"]
    points = [(arrow["x"] + px, arrow["y"] + py) for px, py in arrow["points"]]
    assert len(points) == 3
    assert points[1] == (pytest.approx(254), pytest.approx(-70))
    # The start now leaves the moved box's top edge, towards the middle point.
    assert 0 <= points[0][0] <= 100 and points[0][1] == pytest.approx(196, abs=1)
    # The end still meets the unmoved box.
    assert 396 <= points[2][0] < 400 and 0 <= points[2][1] <= 100


def test_an_empty_text_removes_a_label():
    scene = _two_boxes_and_an_arrow()
    label = next(e for e in scene["elements"] if e.get("containerId") == "left")
    update_elements(scene, [ElementChange(id="left", text="")], now_ms=NOW + 1)
    assert label["isDeleted"] is True
    assert _by_id(scene)["left"]["boundElements"] == [{"id": "link", "type": "arrow"}]


def test_a_label_is_added_to_a_shape_without_one():
    scene = _two_boxes_and_an_arrow()
    update_elements(scene, [ElementChange(id="right", text="New")], now_ms=NOW + 1)
    view = {e.id: e for e in summarize_board(parse_board(serialize_board(scene)))}
    assert view["right"].text == "New"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (ElementChange(id="link", x=5), "follows the elements"),
        (ElementChange(id="nope", x=5), "no element 'nope'"),
        (ElementChange(id="left", font_size=30, text=None, x=None), None),
    ],
)
def test_change_rules(change, message):
    scene = _two_boxes_and_an_arrow()
    if message is None:
        update_elements(scene, [change], now_ms=NOW + 1)
        return
    before = copy.deepcopy(scene)
    with pytest.raises(BoardEditError, match=message):
        update_elements(scene, [change], now_ms=NOW + 1)
    assert scene == before


def test_a_label_cannot_be_changed_directly():
    scene = _two_boxes_and_an_arrow()
    label = next(e for e in scene["elements"] if e.get("containerId") == "left")
    with pytest.raises(BoardEditError, match="is a label"):
        update_elements(scene, [ElementChange(id=label["id"], text="x")], now_ms=NOW)


@pytest.mark.parametrize(
    "extra",
    [
        {"locked": True},
        {"customData": {"type": "comment", "commentThread": {}}},
        {"customData": {"isTable": True}},
    ],
)
def test_locked_elements_comments_and_tables_are_refused(extra):
    scene = _scene({"id": "a", "type": "ellipse", "x": 0, "y": 0, **extra})
    with pytest.raises(BoardEditError):
        update_elements(scene, [ElementChange(id="a", x=10)], now_ms=NOW)
    with pytest.raises(BoardEditError):
        delete_elements(scene, ["a"], now_ms=NOW)


# ── Deleting ────────────────────────────────────────────────────────────


def test_deleting_a_shape_deletes_its_label_and_frees_its_arrows():
    scene = _two_boxes_and_an_arrow()
    label = next(e for e in scene["elements"] if e.get("containerId") == "left")
    count = len(scene["elements"])
    delete_elements(scene, ["left"], now_ms=NOW + 1)
    assert len(scene["elements"]) == count, "nothing is removed from the list"
    assert label["isDeleted"] is True
    arrow = _by_id(scene)["link"]
    assert arrow["startBinding"] is None
    assert arrow["endBinding"]["elementId"] == "right"


def test_deleting_an_arrow_unbinds_both_ends():
    scene = _two_boxes_and_an_arrow()
    delete_elements(scene, ["link"], now_ms=NOW + 1)
    elements = _by_id(scene)
    assert {"id": "link", "type": "arrow"} not in elements["left"]["boundElements"]
    assert elements["right"]["boundElements"] == []


def test_deleting_a_frame_keeps_what_is_in_it():
    scene = _scene()
    add_elements(
        scene,
        [
            NewElement(type="frame", id="f", x=0, y=0, width=500, height=300),
            NewElement(type="rectangle", id="in", x=10, y=10, frame="f"),
        ],
        now_ms=NOW,
    )
    delete_elements(scene, ["f"], now_ms=NOW + 1)
    assert _by_id(scene)["in"]["frameId"] is None


# ── Saving and text ─────────────────────────────────────────────────────


def test_a_board_with_nan_is_not_saved():
    """The browser cannot parse NaN, so the edit is refused rather than saved."""
    with pytest.raises(BoardEditError, match="NaN or Infinity"):
        serialize_board({"elements": [{"x": math.nan}]})


def test_text_is_saved_as_utf8():
    """As the app saves it, so that an edit does not inflate the whole file."""
    content = serialize_board({"elements": [{"text": "Café 計画"}]})
    assert "Café 計画".encode() in content


def test_a_lone_surrogate_survives_saving():
    content = serialize_board({"elements": [{"text": "\ud800"}]})
    assert json.loads(content)["elements"][0]["text"] == "\ud800"


@given(
    text=st.text(alphabet=st.characters(blacklist_categories=["Cs"]), max_size=200),
    width=st.floats(40, 600),
)
def test_wrapping_keeps_the_text_and_fits_where_it_can(text: str, width: float):
    """Only line breaks are added, and only a single character is ever too wide."""
    wrapped = wrap_text(text, width, 20)
    assert wrapped.replace("\n", "") == text.replace("\n", "").replace(" ", "") or (
        wrapped.replace("\n", " ").split() == text.replace("\n", " ").split()
    )
    for line in wrapped.split("\n"):
        assert len(line) <= 1 or measure_text(line, 20)[0] <= math.ceil(width)
