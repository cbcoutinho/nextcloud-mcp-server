"""Editing Nextcloud Whiteboard boards.

The edits work on the scene that :func:`~nextcloud_client.models.whiteboard.parse_board`
returns, in place, and follow the rules that keep a board usable in the
Whiteboard app (Excalidraw 0.18):

- Every key and element the edit does not touch stays as it was, including
  ones from a newer app version and ``customData``.
- A changed element gets a higher ``version``, a new ``versionNonce`` and a new
  ``updated``. The app merges two copies of a board by these.
- Nothing is removed from the element list. A deleted element is marked
  ``isDeleted``, as the app does, so that a copy that still has it does not
  bring it back.
- Both sides of a binding stay in step: a label and its shape, an arrow and the
  two elements it connects.
- New elements are complete, with a fractional ``index`` that sorts after every
  index on the board. The browser rewrites a board whose elements lack one as
  soon as it opens it.
- An element moved or resized out of its frame leaves the frame, as it does
  when dragged in the app. Otherwise the frame would clip it out of sight.

Excalidraw does not measure text when it loads a board, so the size of a text
is estimated here from its characters. The estimate is generous: a text box a
little too wide looks the same as one that fits.
"""

import copy
import json
import math
import secrets
import time
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Most elements one call may add, change or delete.
MAX_ELEMENTS_PER_EDIT = 100
#: Longest text an element may be given.
MAX_EDIT_TEXT_LENGTH = 10_000
#: Coordinates and sizes stay within this, so a typo cannot place an element
#: where nobody will find it.
MAX_COORDINATE = 1_000_000

DEFAULT_SHAPE_WIDTH = 200
DEFAULT_SHAPE_HEIGHT = 100
DEFAULT_FONT_SIZE = 20

# Excalidraw 0.18 defaults.
_EXCALIFONT = 5
_LINE_HEIGHT = 1.25
_BOUND_TEXT_PADDING = 5
_ARROW_LABEL_WIDTH_FRACTION = 0.7
_ARROW_LABEL_MIN_WIDTH_PER_FONT_SIZE = 11
_BINDING_GAP = 4
#: Average advance of a character, as a fraction of the font size. Excalifont
#: is a handwriting font with wide letters; this errs on the wide side.
_CHAR_WIDTH = 0.6
_WIDE_CHAR_WIDTH = 1.0

_SHAPES = ("rectangle", "ellipse", "diamond")
_ROUNDNESS: dict[str, dict[str, int] | None] = {
    "rectangle": {"type": 3},
    "ellipse": {"type": 2},
    "diamond": {"type": 2},
    "arrow": {"type": 2},
    "text": None,
    "frame": None,
}
# Elements an arrow may end at. Excalidraw binds arrows to these.
_BINDABLE = (*_SHAPES, "text", "image", "frame", "magicframe", "embeddable", "iframe")

_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_COLOR_PATTERN = r"^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|transparent)$"
_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"

ElementId = Annotated[str, Field(pattern=_ID_PATTERN)]
Color = Annotated[
    str, Field(pattern=_COLOR_PATTERN, description="#rgb, #rrggbb or 'transparent'")
]
Coordinate = Annotated[
    float, Field(ge=-MAX_COORDINATE, le=MAX_COORDINATE, allow_inf_nan=False)
]
Size = Annotated[float, Field(gt=0, le=MAX_COORDINATE, allow_inf_nan=False)]
FontSize = Annotated[int, Field(ge=8, le=200)]
Text = Annotated[str, Field(max_length=MAX_EDIT_TEXT_LENGTH)]


class BoardEditError(ValueError):
    """A requested change cannot be applied. Nothing was changed."""


class NewElement(BaseModel):
    """An element to add to a board."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["rectangle", "ellipse", "diamond", "text", "arrow", "frame"] = Field(
        description=(
            "rectangle, ellipse and diamond are shapes. text is free text. arrow "
            "connects two elements. frame is a named area that holds others"
        )
    )
    id: ElementId | None = Field(
        default=None,
        description=(
            "Id for the new element, so that an arrow in the same call can "
            "refer to it. Generated when omitted"
        ),
    )
    x: Coordinate | None = Field(
        default=None, description="Left edge. Required except for an arrow"
    )
    y: Coordinate | None = Field(
        default=None, description="Top edge. Required except for an arrow"
    )
    width: Size | None = Field(
        default=None,
        description=f"Shapes default to {DEFAULT_SHAPE_WIDTH}. Required for a frame",
    )
    height: Size | None = Field(
        default=None,
        description=(
            f"Shapes default to {DEFAULT_SHAPE_HEIGHT}, and grow to fit their "
            "label. Required for a frame"
        ),
    )
    text: Text | None = Field(
        default=None,
        description=(
            "A shape's or an arrow's label, a text's content (required), or a "
            "frame's name. Use \\n for a line break"
        ),
    )
    start: ElementId | None = Field(
        default=None, description="Arrow only, required: id of the element it starts at"
    )
    end: ElementId | None = Field(
        default=None, description="Arrow only, required: id of the element it points to"
    )
    frame: ElementId | None = Field(
        default=None,
        description="Id of a frame to put the element in. It must lie inside it",
    )
    stroke_color: Color | None = Field(
        default=None, description="Line colour, or the colour of a text"
    )
    background_color: Color | None = Field(
        default=None, description="Fill colour of a shape"
    )
    font_size: FontSize | None = Field(
        default=None, description=f"Of the text or label. Default {DEFAULT_FONT_SIZE}"
    )


class ElementChange(BaseModel):
    """Changes to one element. Fields left out stay as they are."""

    model_config = ConfigDict(extra="forbid")

    id: ElementId = Field(description="Id of the element to change")
    text: Text | None = Field(
        default=None,
        description=(
            "New label of a shape or an arrow (an empty string removes it), "
            "content of a text, or name of a frame"
        ),
    )
    x: Coordinate | None = Field(
        default=None, description="New left edge. Not for an arrow"
    )
    y: Coordinate | None = Field(
        default=None, description="New top edge. Not for an arrow"
    )
    width: Size | None = Field(default=None, description="Shapes and frames only")
    height: Size | None = Field(default=None, description="Shapes and frames only")
    stroke_color: Color | None = None
    background_color: Color | None = None
    font_size: FontSize | None = Field(
        default=None, description="Of a text, or of the label of a shape or an arrow"
    )


# ── Serialising ─────────────────────────────────────────────────────────


def serialize_board(document: dict[str, Any]) -> bytes:
    """The bytes to save for a board document.

    Raises:
        BoardEditError: The board holds NaN or Infinity, which the browser
            cannot read back as JSON.
    """
    try:
        text = json.dumps(document, ensure_ascii=False, allow_nan=False)
    except ValueError as e:
        raise BoardEditError(
            "the board holds a number JSON cannot represent (NaN or Infinity)"
        ) from e
    # UTF-8, as the app writes it. A lone surrogate, which the JSON we read may
    # hold as an escape, has no UTF-8 form: it is written back as the same
    # escape. That is valid JSON, since a surrogate only occurs in a string.
    return text.encode("utf-8", "backslashreplace")


# ── Fractional indices ──────────────────────────────────────────────────
# Excalidraw orders elements by keys from the fractional-indexing library
# (https://github.com/rocicorp/fractional-indexing, CC0). Only "a key after
# this one" is needed here, since new elements always go at the end.

_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _integer_length(head: str) -> int:
    if "a" <= head <= "z":
        return ord(head) - ord("a") + 2
    if "A" <= head <= "Z":
        return ord("Z") - ord(head) + 2
    raise ValueError(f"invalid order key head: {head!r}")


def _is_valid_key(key: str) -> bool:
    # The smallest possible key is reserved, as in the library.
    if not key or key == "A" + "0" * 26:
        return False
    try:
        length = _integer_length(key[0])
    except ValueError:
        return False
    if len(key) < length or any(c not in _DIGITS for c in key[1:]):
        return False
    fraction = key[length:]
    return not fraction.endswith("0")


def _increment_integer(integer: str) -> str | None:
    head, digits = integer[0], list(integer[1:])
    carry = True
    for i in range(len(digits) - 1, -1, -1):
        if not carry:
            break
        position = _DIGITS.index(digits[i]) + 1
        if position == len(_DIGITS):
            digits[i] = "0"
        else:
            digits[i] = _DIGITS[position]
            carry = False
    if not carry:
        return head + "".join(digits)
    if head == "Z":
        return "a0"
    if head == "z":
        return None
    new_head = chr(ord(head) + 1)
    if new_head > "a":
        digits.append("0")
    else:
        digits.pop()
    return new_head + "".join(digits)


def _key_after(key: str | None) -> str:
    if key is None:
        return "a0"
    incremented = _increment_integer(key[: _integer_length(key[0])])
    if incremented is not None:
        return incremented
    # The largest integer: any longer key that starts with this one sorts
    # after it, and a key may not end in "0".
    return key + "V"


def _next_index(elements: list[Any]) -> str:
    """A valid key that sorts after every index on the board.

    Excalidraw compares indices as plain strings, malformed ones included, so
    the new key must exceed those too. Past a key that starts with "z" or a
    later character nothing valid sorts higher: the browser then reorders.
    """
    keys = [
        e["index"]
        for e in elements
        if isinstance(e, dict) and isinstance(e.get("index"), str) and e["index"]
    ]
    valid = [key for key in keys if _is_valid_key(key)]
    key = _key_after(max(valid) if valid else None)
    top = max(keys) if keys else None
    if top is not None and key <= top and "a" <= top[0] < "z":
        head = chr(ord(top[0]) + 1)
        key = head + "0" * (_integer_length(head) - 1)
    return key


# ── Text ────────────────────────────────────────────────────────────────


def _char_width(char: str) -> float:
    wide = unicodedata.east_asian_width(char) in ("W", "F")
    return _WIDE_CHAR_WIDTH if wide else _CHAR_WIDTH


def _line_width(line: str, font_size: float) -> float:
    return sum(_char_width(c) for c in line) * font_size


def measure_text(text: str, font_size: float) -> tuple[float, float]:
    """Estimated (width, height) of ``text`` in Excalifont."""
    lines = text.split("\n")
    width = max(_line_width(line, font_size) for line in lines)
    return math.ceil(width), math.ceil(len(lines) * font_size * _LINE_HEIGHT)


def wrap_text(text: str, max_width: float, font_size: float) -> str:
    """``text`` with line breaks added so that no line exceeds ``max_width``."""
    return "\n".join(
        line
        for paragraph in text.split("\n")
        for line in _wrap_paragraph(paragraph, max_width, font_size)
    )


def _wrap_paragraph(paragraph: str, max_width: float, font_size: float) -> list[str]:
    wrapped: list[str] = []
    line = ""
    for word in paragraph.split(" "):
        candidate = f"{line} {word}" if line else word
        if _line_width(candidate, font_size) <= max_width:
            line = candidate
            continue
        if line:
            wrapped.append(line)
        pieces = _break_word(word, max_width, font_size)
        wrapped.extend(pieces[:-1])
        line = pieces[-1]
    wrapped.append(line)
    return wrapped


def _break_word(word: str, max_width: float, font_size: float) -> list[str]:
    """A word longer than the line, broken between characters. The last piece
    starts the next line."""
    pieces = [""]
    for char in word:
        if pieces[-1] and _line_width(pieces[-1] + char, font_size) > max_width:
            pieces.append("")
        pieces[-1] += char
    return pieces


# ── Geometry ────────────────────────────────────────────────────────────


def _box(element: dict[str, Any]) -> tuple[float, float, float, float]:
    """(x, y, width, height) of an element, with missing values as 0."""

    def number(key: str) -> float:
        value = element.get(key)
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return float(value) if ok and math.isfinite(value) else 0.0

    return number("x"), number("y"), number("width"), number("height")


def _center(element: dict[str, Any]) -> tuple[float, float]:
    x, y, width, height = _box(element)
    return x + width / 2, y + height / 2


def _edge_point(
    element: dict[str, Any], toward: tuple[float, float]
) -> tuple[float, float]:
    """Where a line from the element's centre toward ``toward`` leaves it,
    plus the binding gap."""
    cx, cy = _center(element)
    dx, dy = toward[0] - cx, toward[1] - cy
    distance = math.hypot(dx, dy)
    if distance == 0:
        return cx, cy
    ux, uy = dx / distance, dy / distance
    _, _, width, height = _box(element)
    hw, hh = max(width / 2, 1e-9), max(height / 2, 1e-9)
    if element.get("type") == "ellipse":
        reach = 1 / math.hypot(ux / hw, uy / hh)
    elif element.get("type") == "diamond":
        reach = 1 / (abs(ux) / hw + abs(uy) / hh)
    else:
        reach = min(hw / abs(ux) if ux else math.inf, hh / abs(uy) if uy else math.inf)
    reach = min(reach + _BINDING_GAP, distance)
    return cx + ux * reach, cy + uy * reach


def _contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    ox, oy, ow, oh = _box(outer)
    ix, iy, iw, ih = _box(inner)
    return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh


# ── The scene ───────────────────────────────────────────────────────────


class _Scene:
    """Index of a scene's live elements, and the stamp for changed ones."""

    def __init__(self, scene: dict[str, Any], now_ms: int | None = None) -> None:
        self.elements: list[Any] = scene["elements"]
        self.now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self.by_id: dict[str, dict[str, Any]] = {}
        self.ambiguous: set[str] = set()
        self.all_ids: set[str] = set()
        for element in self.elements:
            if not isinstance(element, dict) or not isinstance(element.get("id"), str):
                continue
            element_id = element["id"]
            self.all_ids.add(element_id)
            if element.get("isDeleted") is True:
                continue
            if element_id in self.by_id:
                self.ambiguous.add(element_id)
            self.by_id[element_id] = element
        self.touched: dict[int, dict[str, Any]] = {}

    def get(self, element_id: str, purpose: str) -> dict[str, Any]:
        if element_id in self.ambiguous:
            raise BoardEditError(
                f"{element_id!r} names more than one element on this board"
            )
        element = self.by_id.get(element_id)
        if element is None:
            raise BoardEditError(f"no element {element_id!r} to {purpose}")
        return element

    def touch(self, element: dict[str, Any]) -> None:
        """Mark ``element`` changed, once per edit."""
        if id(element) in self.touched:
            return
        version = element.get("version")
        ok = isinstance(version, int) and not isinstance(version, bool)
        element["version"] = (version if ok and version > 0 else 0) + 1
        element["versionNonce"] = _random_int()
        element["updated"] = self.now_ms
        self.touched[id(element)] = element

    def new_id(self) -> str:
        while True:
            element_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(21))
            if element_id not in self.all_ids:
                self.all_ids.add(element_id)
                return element_id

    def append(self, element: dict[str, Any]) -> None:
        element["index"] = _next_index(self.elements)
        self.elements.append(element)
        self.by_id[element["id"]] = element
        self.all_ids.add(element["id"])

    def label_of(self, container: dict[str, Any]) -> dict[str, Any] | None:
        for bound in _bound(container):
            if bound.get("type") == "text":
                label = self.by_id.get(str(bound.get("id")))
                if label is not None and label.get("containerId") == container["id"]:
                    return label
        return None

    def arrows_of(self, element: dict[str, Any]) -> list[dict[str, Any]]:
        arrows = []
        for bound in _bound(element):
            arrow = self.by_id.get(str(bound.get("id")))
            if arrow is not None and arrow.get("type") == "arrow":
                arrows.append(arrow)
        return arrows


def _random_int() -> int:
    return secrets.randbelow(2**31 - 1) + 1


def _bound(element: dict[str, Any]) -> list[dict[str, Any]]:
    bound = element.get("boundElements")
    return [b for b in bound if isinstance(b, dict)] if isinstance(bound, list) else []


def _add_bound(element: dict[str, Any], bound_id: str, bound_type: str) -> None:
    bound = _bound(element)
    if not any(b.get("id") == bound_id for b in bound):
        bound.append({"id": bound_id, "type": bound_type})
    element["boundElements"] = bound


def _remove_bound(element: dict[str, Any], bound_id: str) -> bool:
    bound = _bound(element)
    kept = [b for b in bound if b.get("id") != bound_id]
    if len(kept) == len(bound):
        return False
    element["boundElements"] = kept
    return True


def _base(scene: _Scene, element_type: str, element_id: str) -> dict[str, Any]:
    return {
        "id": element_id,
        "type": element_type,
        "x": 0,
        "y": 0,
        "width": 0,
        "height": 0,
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "index": None,
        "roundness": _ROUNDNESS.get(element_type),
        "seed": _random_int(),
        "version": 1,
        "versionNonce": _random_int(),
        "isDeleted": False,
        "boundElements": [],
        "updated": scene.now_ms,
        "link": None,
        "locked": False,
    }


def _text_element(
    scene: _Scene, text: str, font_size: int, container_id: str | None
) -> dict[str, Any]:
    element = _base(scene, "text", scene.new_id())
    element.update(
        {
            "text": text,
            "originalText": text,
            "fontSize": font_size,
            "fontFamily": _EXCALIFONT,
            "textAlign": "center" if container_id else "left",
            "verticalAlign": "middle" if container_id else "top",
            "containerId": container_id,
            "lineHeight": _LINE_HEIGHT,
            "autoResize": True,
            "boundElements": None,
        }
    )
    return element


def _font_size(element: dict[str, Any]) -> int:
    size = element.get("fontSize")
    ok = isinstance(size, (int, float)) and not isinstance(size, bool)
    return int(size) if ok and 8 <= size <= 200 else DEFAULT_FONT_SIZE


def _original_text(element: dict[str, Any]) -> str:
    for key in ("originalText", "text"):
        value = element.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _label_max_width(container: dict[str, Any], font_size: int) -> float:
    _, _, width, _ = _box(container)
    kind = container.get("type")
    if kind == "arrow":
        return max(
            _ARROW_LABEL_WIDTH_FRACTION * width,
            _ARROW_LABEL_MIN_WIDTH_PER_FONT_SIZE * font_size,
        )
    if kind == "ellipse":
        return round(width / 2 * math.sqrt(2)) - 2 * _BOUND_TEXT_PADDING
    if kind == "diamond":
        return round(width / 2) - 2 * _BOUND_TEXT_PADDING
    return width - 2 * _BOUND_TEXT_PADDING


def _label_max_height(container: dict[str, Any]) -> float:
    _, _, _, height = _box(container)
    kind = container.get("type")
    if kind == "ellipse":
        return round(height / 2 * math.sqrt(2)) - 2 * _BOUND_TEXT_PADDING
    if kind == "diamond":
        return round(height / 2) - 2 * _BOUND_TEXT_PADDING
    return height - 2 * _BOUND_TEXT_PADDING


def _container_height_for(label_height: float, kind: str) -> float:
    padded = label_height + 2 * _BOUND_TEXT_PADDING
    if kind == "ellipse":
        return round(padded / math.sqrt(2) * 2)
    if kind == "diamond":
        return padded * 2
    return padded


def _arrow_midpoint(arrow: dict[str, Any]) -> tuple[float, float]:
    points = _absolute_points(arrow)
    if len(points) % 2 == 1:
        return points[len(points) // 2]
    a, b = points[len(points) // 2 - 1], points[len(points) // 2]
    return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2


def _layout_label(
    scene: _Scene, container: dict[str, Any], label: dict[str, Any]
) -> None:
    """Wrap a label to its container, grow the container to fit, and centre it."""
    font_size = _font_size(label)
    text = wrap_text(
        _original_text(label), _label_max_width(container, font_size), font_size
    )
    width, height = measure_text(text, font_size)
    kind = str(container.get("type"))
    if kind != "arrow" and height > _label_max_height(container):
        container["height"] = _container_height_for(height, kind)
        scene.touch(container)
    if kind == "arrow":
        cx, cy = _arrow_midpoint(container)
    else:
        cx, cy = _center(container)
    label.update(
        {
            "text": text,
            "width": width,
            "height": height,
            "x": cx - width / 2,
            "y": cy - height / 2,
            "frameId": container.get("frameId"),
        }
    )
    scene.touch(label)


def _set_label(
    scene: _Scene, container: dict[str, Any], text: str, font_size: int | None
) -> None:
    label = scene.label_of(container)
    if not text:
        if label is not None:
            label["isDeleted"] = True
            scene.touch(label)
            _remove_bound(container, label["id"])
            scene.touch(container)
        return
    if label is None:
        label = _text_element(
            scene, text, font_size or DEFAULT_FONT_SIZE, container["id"]
        )
        scene.append(label)
        _add_bound(container, label["id"], "text")
        scene.touch(container)
    else:
        label["originalText"] = text
        if font_size is not None:
            label["fontSize"] = font_size
    _layout_label(scene, container, label)


def _absolute_points(arrow: dict[str, Any]) -> list[tuple[float, float]]:
    x, y, _, _ = _box(arrow)
    points = arrow.get("points")
    result = []
    if isinstance(points, list):
        for point in points:
            if (
                isinstance(point, list)
                and len(point) == 2
                and all(
                    isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and math.isfinite(v)
                    for v in point
                )
            ):
                result.append((x + point[0], y + point[1]))
    return result if len(result) >= 2 else [(x, y), (x, y)]


def _set_points(arrow: dict[str, Any], points: list[tuple[float, float]]) -> None:
    x0, y0 = points[0]
    relative = [[round(px - x0, 2), round(py - y0, 2)] for px, py in points]
    xs = [p[0] for p in relative]
    ys = [p[1] for p in relative]
    arrow.update(
        {
            "x": round(x0, 2),
            "y": round(y0, 2),
            "points": relative,
            "width": max(xs) - min(xs),
            "height": max(ys) - min(ys),
        }
    )


def _binding_target(
    scene: _Scene, arrow: dict[str, Any], key: str
) -> dict[str, Any] | None:
    binding = arrow.get(key)
    if not isinstance(binding, dict):
        return None
    return scene.by_id.get(str(binding.get("elementId")))


def _route(scene: _Scene, arrow: dict[str, Any]) -> None:
    """Point the bound ends of an arrow at the elements they are bound to."""
    start = _binding_target(scene, arrow, "startBinding")
    end = _binding_target(scene, arrow, "endBinding")
    if start is None and end is None:
        return
    points = _absolute_points(arrow)
    straight = len(points) == 2 and start is not None and end is not None
    if start is not None:
        toward = _center(end) if straight and end is not None else points[1]
        points[0] = _edge_point(start, toward)
    if end is not None:
        toward = _center(start) if straight and start is not None else points[-2]
        points[-1] = _edge_point(end, toward)
    _set_points(arrow, points)
    scene.touch(arrow)
    label = scene.label_of(arrow)
    if label is not None:
        _layout_label(scene, arrow, label)


# ── Operations ──────────────────────────────────────────────────────────


def _check_count(items: list[Any], what: str) -> None:
    if not items:
        raise BoardEditError(f"no {what} given")
    if len(items) > MAX_ELEMENTS_PER_EDIT:
        raise BoardEditError(
            f"{len(items)} {what} given; at most {MAX_ELEMENTS_PER_EDIT} per call"
        )


def add_elements(
    scene_dict: dict[str, Any], new: list[NewElement], *, now_ms: int | None = None
) -> list[str]:
    """Add elements to a scene. Returns their ids, in the order given.

    Raises:
        BoardEditError: An element cannot be added. The scene is unchanged.
    """
    _check_count(new, "elements")
    # Everything is checked before the scene changes, so an error leaves it as
    # it was.
    scene = _Scene(scene_dict, now_ms)
    staged = _stage(scene, new)
    pending = {element["id"]: element for _, element in staged}
    for item, element in staged:
        _fill_new(item, element)
    for item, element in staged:
        if item.frame is not None:
            _put_in_frame(scene, pending, item, element)
        if item.type == "arrow":
            _bind_ends(scene, pending, item, element)

    # Everything is valid: change the scene.
    for _, element in staged:
        scene.append(element)
    for item, element in staged:
        _connect(scene, pending, item, element)
    return [element["id"] for _, element in staged]


def _stage(
    scene: _Scene, new: list[NewElement]
) -> list[tuple[NewElement, dict[str, Any]]]:
    """A base element for each new one, with its id, not yet in the scene."""
    staged: list[tuple[NewElement, dict[str, Any]]] = []
    for item in new:
        element_id = item.id or scene.new_id()
        if item.id is not None:
            if item.id in scene.all_ids:
                raise BoardEditError(f"the board already has an element {item.id!r}")
            scene.all_ids.add(item.id)
        staged.append((item, _base(scene, item.type, element_id)))
    return staged


def _lookup(
    scene: _Scene, pending: dict[str, dict[str, Any]], element_id: str, purpose: str
) -> dict[str, Any]:
    """An element added in the same call, or one already on the board."""
    if element_id in pending:
        return pending[element_id]
    return scene.get(element_id, purpose)


def _put_in_frame(
    scene: _Scene,
    pending: dict[str, dict[str, Any]],
    item: NewElement,
    element: dict[str, Any],
) -> None:
    frame_id = str(item.frame)
    frame = _lookup(scene, pending, frame_id, "put it in")
    if frame.get("type") not in ("frame", "magicframe"):
        raise BoardEditError(f"{frame_id!r} is not a frame")
    if item.type != "arrow" and not _contains(frame, element):
        raise BoardEditError(
            f"element {element['id']!r} does not lie inside frame {frame_id!r}"
        )
    element["frameId"] = frame_id


def _bind_ends(
    scene: _Scene,
    pending: dict[str, dict[str, Any]],
    item: NewElement,
    element: dict[str, Any],
) -> None:
    """Set a new arrow's bindings, after checking both ends."""
    for key, ref in (("startBinding", item.start), ("endBinding", item.end)):
        if ref is None:  # _fill_new refuses this: kept for the type checker
            raise BoardEditError("an arrow needs start and end")
        target = _lookup(scene, pending, ref, "connect")
        if target.get("type") not in _BINDABLE or target.get("containerId"):
            raise BoardEditError(f"an arrow cannot end at {ref!r}")
        element[key] = {"elementId": ref, "focus": 0, "gap": _BINDING_GAP}
    if item.start == item.end:
        raise BoardEditError("an arrow needs two different elements")


def _connect(
    scene: _Scene,
    pending: dict[str, dict[str, Any]],
    item: NewElement,
    element: dict[str, Any],
) -> None:
    """Bind a new arrow to the elements at its ends, and label a new element."""
    if item.type == "arrow":
        for ref in (item.start, item.end):
            target = scene.by_id[str(ref)]
            _add_bound(target, element["id"], "arrow")
            if target["id"] not in pending:
                scene.touch(target)
        _route(scene, element)
    if item.text and item.type in (*_SHAPES, "arrow"):
        _set_label(scene, element, item.text, item.font_size)


def _fill_new(item: NewElement, element: dict[str, Any]) -> None:
    if item.type == "arrow":
        _fill_arrow(item, element)
    else:
        _place(item, element)
    _size(item, element)
    if item.stroke_color:
        element["strokeColor"] = item.stroke_color
    if item.background_color:
        element["backgroundColor"] = item.background_color


def _fill_arrow(item: NewElement, element: dict[str, Any]) -> None:
    if item.start is None or item.end is None:
        raise BoardEditError("an arrow needs start and end")
    if item.x is not None or item.y is not None or item.width or item.height:
        raise BoardEditError(
            "an arrow takes no position or size: it runs between start and end"
        )
    element.update(
        {
            "points": [[0, 0], [0, 0]],
            "lastCommittedPoint": None,
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": None,
            "endArrowhead": "arrow",
            "elbowed": False,
        }
    )


def _place(item: NewElement, element: dict[str, Any]) -> None:
    if item.start is not None or item.end is not None:
        raise BoardEditError(f"only an arrow takes start and end, not {item.type!r}")
    if item.x is None or item.y is None:
        raise BoardEditError(f"{item.type!r} needs x and y")
    element["x"], element["y"] = item.x, item.y


def _size(item: NewElement, element: dict[str, Any]) -> None:
    if item.type in _SHAPES:
        element["width"] = item.width or DEFAULT_SHAPE_WIDTH
        element["height"] = item.height or DEFAULT_SHAPE_HEIGHT
    elif item.type == "frame":
        if item.width is None or item.height is None:
            raise BoardEditError("a frame needs width and height")
        element.update(
            {"width": item.width, "height": item.height, "name": item.text or None}
        )
    elif item.type == "text":
        _fill_text(item, element)


def _fill_text(item: NewElement, element: dict[str, Any]) -> None:
    if not item.text:
        raise BoardEditError("a text needs text")
    if item.width or item.height:
        raise BoardEditError("a text takes no size: it is sized to its content")
    element.update(_text_fields(item.text, item.font_size or DEFAULT_FONT_SIZE))
    element["boundElements"] = None


def _text_fields(text: str, font_size: int) -> dict[str, Any]:
    width, height = measure_text(text, font_size)
    return {
        "text": text,
        "originalText": text,
        "fontSize": font_size,
        "fontFamily": _EXCALIFONT,
        "textAlign": "left",
        "verticalAlign": "top",
        "containerId": None,
        "lineHeight": _LINE_HEIGHT,
        "autoResize": True,
        "width": width,
        "height": height,
    }


def _editable(scene: _Scene, element_id: str, purpose: str) -> dict[str, Any]:
    element = scene.get(element_id, purpose)
    if element.get("locked") is True:
        raise BoardEditError(f"{element_id!r} is locked on the board")
    custom = element.get("customData")
    if isinstance(custom, dict) and (
        custom.get("type") == "comment" or custom.get("isTable") is True
    ):
        raise BoardEditError(
            f"{element_id!r} is a comment or a table, which only the app can change"
        )
    return element


def update_elements(
    scene_dict: dict[str, Any],
    changes: list[ElementChange],
    *,
    now_ms: int | None = None,
) -> list[str]:
    """Apply changes to elements of a scene. Returns the ids of every element
    that changed, including labels and arrows that followed.

    Raises:
        BoardEditError: A change cannot be applied. The scene is unchanged.
    """
    _check_count(changes, "changes")
    ids = [change.id for change in changes]
    if len(set(ids)) != len(ids):
        raise BoardEditError("an element is named in more than one change")
    # A change can fail after others in the same call were applied. deepcopy,
    # unlike a JSON round trip, restores values such as NaN exactly.
    snapshot = copy.deepcopy(scene_dict["elements"])
    scene = _Scene(scene_dict, now_ms)
    try:
        moved: dict[str, tuple[float, float]] = {}
        for change in changes:
            element = _editable(scene, change.id, "change")
            _apply_change(scene, element, change, moved)
        _move_closure(scene, moved)
        _leave_frames(scene)
    except BoardEditError:
        scene_dict["elements"][:] = snapshot
        raise
    return [str(e.get("id")) for e in scene.touched.values()]


def _apply_change(
    scene: _Scene,
    element: dict[str, Any],
    change: ElementChange,
    moved: dict[str, tuple[float, float]],
) -> None:
    _check_change(element, change)
    _change_box(scene, element, change, moved)
    if change.stroke_color is not None:
        element["strokeColor"] = change.stroke_color
        scene.touch(element)
    if change.background_color is not None:
        element["backgroundColor"] = change.background_color
        scene.touch(element)
    _change_text(scene, element, change)
    if _resizes(change):
        for arrow in scene.arrows_of(element):
            _route(scene, arrow)


def _resizes(change: ElementChange) -> bool:
    return change.width is not None or change.height is not None


def _given(value: float | None, current: float) -> float:
    return current if value is None else value


def _check_change(element: dict[str, Any], change: ElementChange) -> None:
    """Refuse a change the element cannot take, before anything is changed."""
    kind = element.get("type")
    if element.get("containerId"):
        raise BoardEditError(
            f"{change.id!r} is a label: change the text of its shape or arrow instead"
        )
    geometry = (change.x, change.y, change.width, change.height)
    if kind == "arrow" and any(v is not None for v in geometry):
        raise BoardEditError("an arrow follows the elements it connects: move those")
    if kind == "text" and _resizes(change):
        raise BoardEditError("a text is sized to its content")
    if kind not in (*_SHAPES, "frame", "magicframe") and _resizes(change):
        raise BoardEditError(f"{kind!r} cannot be resized here")


def _change_box(
    scene: _Scene,
    element: dict[str, Any],
    change: ElementChange,
    moved: dict[str, tuple[float, float]],
) -> None:
    """Resize the element now, and record a move for ``_move_closure``."""
    x, y, width, height = _box(element)
    if change.x is not None or change.y is not None:
        dx = _given(change.x, x) - x
        dy = _given(change.y, y) - y
        if dx or dy:
            moved[str(element["id"])] = (dx, dy)
    if _resizes(change):
        element["width"] = _given(change.width, width)
        element["height"] = _given(change.height, height)
        scene.touch(element)


def _change_text(scene: _Scene, element: dict[str, Any], change: ElementChange) -> None:
    kind = element.get("type")
    if kind == "text":
        _change_text_element(scene, element, change)
    elif kind in ("frame", "magicframe"):
        _rename_frame(scene, element, change)
    elif kind in (*_SHAPES, "arrow"):
        _change_label(scene, element, change)
    elif change.text is not None or change.font_size is not None:
        raise BoardEditError(f"{kind!r} has no text to change")


def _change_text_element(
    scene: _Scene, element: dict[str, Any], change: ElementChange
) -> None:
    if change.text is None and change.font_size is None:
        return
    text = _original_text(element) if change.text is None else change.text
    if not text:
        raise BoardEditError("a text cannot be empty: delete it instead")
    font_size = change.font_size or _font_size(element)
    element.update(
        {
            k: v
            for k, v in _text_fields(text, font_size).items()
            if k in ("text", "originalText", "fontSize", "width", "height")
        }
    )
    scene.touch(element)


def _rename_frame(
    scene: _Scene, element: dict[str, Any], change: ElementChange
) -> None:
    if change.font_size is not None:
        raise BoardEditError("a frame has no font size")
    if change.text is not None:
        element["name"] = change.text or None
        scene.touch(element)


def _change_label(
    scene: _Scene, element: dict[str, Any], change: ElementChange
) -> None:
    """Change a shape's or an arrow's label, or lay it out again after a resize."""
    if change.text is not None:
        _set_label(scene, element, change.text, change.font_size)
        return
    label = scene.label_of(element)
    if change.font_size is not None:
        if label is None:
            raise BoardEditError(f"{change.id!r} has no label to resize")
        label["fontSize"] = change.font_size
        _layout_label(scene, element, label)
    elif label is not None and _resizes(change):
        _layout_label(scene, element, label)


def _move_closure(scene: _Scene, moved: dict[str, tuple[float, float]]) -> None:
    """Move elements, with their labels, frame members and arrows."""
    if not moved:
        return
    shift = _with_labels(scene, _with_frame_members(scene, moved))
    arrows: dict[int, dict[str, Any]] = {}
    for element_id, (dx, dy) in shift.items():
        element = scene.by_id[element_id]
        if element.get("type") == "arrow":
            arrows[id(element)] = element
            continue
        _translate(scene, element, dx, dy)
        for arrow in scene.arrows_of(element):
            arrows[id(arrow)] = arrow
    for arrow in arrows.values():
        if arrow["id"] in shift:
            # An arrow inside a moved frame moves with it; its bound ends are
            # then pointed at their elements again.
            _translate(scene, arrow, *shift[arrow["id"]])
        _route(scene, arrow)


def _with_frame_members(
    scene: _Scene, moved: dict[str, tuple[float, float]]
) -> dict[str, tuple[float, float]]:
    """``moved``, plus the members of each moved frame that did not move
    themselves."""
    shift: dict[str, tuple[float, float]] = {}
    for element_id, delta in moved.items():
        shift[element_id] = delta
        if scene.by_id[element_id].get("type") not in ("frame", "magicframe"):
            continue
        for member in scene.by_id.values():
            if member.get("frameId") == element_id and member["id"] not in moved:
                shift.setdefault(member["id"], delta)
    return shift


def _with_labels(
    scene: _Scene, shift: dict[str, tuple[float, float]]
) -> dict[str, tuple[float, float]]:
    """``shift``, plus the label of each element in it."""
    labels: dict[str, tuple[float, float]] = {}
    for element_id, delta in shift.items():
        label = scene.label_of(scene.by_id[element_id])
        if label is not None:
            labels.setdefault(label["id"], delta)
    for label_id, delta in labels.items():
        shift.setdefault(label_id, delta)
    return shift


def _translate(scene: _Scene, element: dict[str, Any], dx: float, dy: float) -> None:
    x, y, _, _ = _box(element)
    element["x"], element["y"] = x + dx, y + dy
    scene.touch(element)


def _leave_frames(scene: _Scene) -> None:
    """Take a changed element out of a frame it no longer lies inside."""
    leaving = [e for e in scene.touched.values() if _outside_frame(scene, e)]
    for element in leaving:
        element["frameId"] = None
        label = scene.label_of(element)
        if label is not None:
            label["frameId"] = None
            scene.touch(label)


def _outside_frame(scene: _Scene, element: dict[str, Any]) -> bool:
    """Whether an element has a frame and no longer lies inside it. A label
    follows its container, and an arrow's frame is left as it is."""
    frame_id = element.get("frameId")
    if (
        not isinstance(frame_id, str)
        or element.get("type") == "arrow"
        or element.get("containerId")
        or element.get("isDeleted") is True
    ):
        return False
    frame = scene.by_id.get(frame_id)
    return frame is not None and not _contains(frame, element)


def delete_elements(
    scene_dict: dict[str, Any], ids: list[str], *, now_ms: int | None = None
) -> list[str]:
    """Mark elements deleted, with their labels. Returns every changed id.

    Arrows that end at a deleted element stay, unbound at that end. Members of a
    deleted frame stay, outside any frame.

    Raises:
        BoardEditError: An element cannot be deleted. The scene is unchanged.
    """
    _check_count(ids, "ids")
    scene = _Scene(scene_dict, now_ms)
    targets = []
    for element_id in dict.fromkeys(ids):
        element = _editable(scene, element_id, "delete")
        if element.get("containerId"):
            raise BoardEditError(
                f"{element_id!r} is a label: set its shape's text to '' instead"
            )
        targets.append(element)
    for element in targets:
        _delete(scene, element)
    return [str(e.get("id")) for e in scene.touched.values()]


def _delete(scene: _Scene, element: dict[str, Any]) -> None:
    if element.get("isDeleted") is True and id(element) in scene.touched:
        return
    element["isDeleted"] = True
    scene.touch(element)
    label = scene.label_of(element)
    if label is not None:
        label["isDeleted"] = True
        scene.touch(label)
    if element.get("type") == "arrow":
        _unbind_ends(scene, element)
    _unbind_arrows(scene, element)
    if element.get("type") in ("frame", "magicframe"):
        _free_members(scene, element["id"])


def _unbind_ends(scene: _Scene, arrow: dict[str, Any]) -> None:
    """Take a deleted arrow off the elements at its ends."""
    for key in ("startBinding", "endBinding"):
        target = _binding_target(scene, arrow, key)
        if target is not None and _remove_bound(target, arrow["id"]):
            scene.touch(target)


def _unbind_arrows(scene: _Scene, element: dict[str, Any]) -> None:
    """Leave the arrows that end at a deleted element unbound at that end."""
    for arrow in scene.arrows_of(element):
        for key in ("startBinding", "endBinding"):
            binding = arrow.get(key)
            if isinstance(binding, dict) and binding.get("elementId") == element["id"]:
                arrow[key] = None
                scene.touch(arrow)


def _free_members(scene: _Scene, frame_id: str) -> None:
    """Take the members of a deleted frame out of it."""
    for member in scene.by_id.values():
        if member.get("frameId") == frame_id and member.get("isDeleted") is not True:
            member["frameId"] = None
            scene.touch(member)
