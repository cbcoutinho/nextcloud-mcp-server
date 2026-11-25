"""Reading Nextcloud Whiteboard boards.

A board is one JSON file in the Excalidraw format, so there is no app API to
call: the file comes over WebDAV and this module understands its content.

A board file is far larger than what it says. Every element carries styling,
seeds and versions, and embedded images are stored inline as base64.
:func:`summarize_board` keeps what a reader needs and drops the rest.

Board files are untrusted input: anyone with write access to the file can put
anything in it, and apps other than Whiteboard write the format too. Parsing
raises only :class:`InvalidBoardError`, and an element that is malformed in one
field loses that field rather than failing the whole board.
"""

import json
import math
from html.parser import HTMLParser
from typing import Any

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

#: The MIME type the Whiteboard app registers for ``.whiteboard`` files.
WHITEBOARD_MIME_TYPE = "application/vnd.excalidraw+json"

# Bounds on one summarized element, so that a page of them is bounded too.
#: Longer text is cut off, and the element says so.
MAX_TEXT_LENGTH = 10_000
#: A longer link is dropped: half a link is worse than none.
MAX_LINK_LENGTH = 2_048
#: Ids and types are short (Excalidraw generates 21 characters). A longer one is
#: treated as malformed, since cutting it would name a different element.
MAX_ID_LENGTH = 256
#: Nesting depth of groups. Further ones are dropped.
MAX_GROUPS = 20


class InvalidBoardError(ValueError):
    """The content is not a whiteboard."""


class WhiteboardElement(BaseModel):
    """One visible element of a board, without styling.

    Absent fields are left out when serialized. A board holds hundreds of
    elements and most fields apply to a few of them, so the nulls would be most
    of the response.
    """

    id: str = Field(description="Element id, unique within the board")
    type: str = Field(
        description=(
            "Excalidraw element type (rectangle, ellipse, diamond, text, arrow, "
            "line, freedraw, image, frame, ...), or 'table' / 'comment' for "
            "those two Whiteboard features"
        )
    )
    text: str | None = Field(
        default=None,
        description=(
            "What the element says: a text's content, the label of a shape or "
            "arrow, a frame's name, a table's cells or a comment thread"
        ),
    )
    text_truncated: bool | None = Field(
        default=None,
        description=f"True if text was cut off at {MAX_TEXT_LENGTH} characters",
    )
    x: int | None = Field(default=None, description="Left edge, rounded")
    y: int | None = Field(default=None, description="Top edge, rounded")
    width: int | None = Field(default=None, description="Width, rounded")
    height: int | None = Field(default=None, description="Height, rounded")
    start: str | None = Field(
        default=None, description="Id of the element an arrow starts at"
    )
    end: str | None = Field(
        default=None, description="Id of the element an arrow points to"
    )
    frame: str | None = Field(
        default=None, description="Id of the frame this element is in"
    )
    groups: list[str] | None = Field(
        default=None, description="Ids of the groups it belongs to, innermost first"
    )
    link: str | None = Field(default=None, description="Link attached to the element")

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return {key: value for key, value in handler(self).items() if value is not None}


def parse_board(content: bytes) -> dict[str, Any]:
    """The scene of a board file: a dict with at least an ``elements`` list.

    A new board is an empty file. An older file may wrap the scene in ``data``.
    Every other key is returned unchanged.

    Raises:
        InvalidBoardError: The content is not a board.
    """
    if not content.strip():
        return {"elements": []}
    try:
        scene = json.loads(content)
    # RecursionError: json.loads recurses per nesting level, and a few thousand
    # brackets are enough. ValueError covers bad JSON, bad UTF-8 and integers
    # over Python's digit limit.
    except (ValueError, RecursionError) as e:
        raise InvalidBoardError("not valid JSON") from e

    if isinstance(scene, dict) and isinstance(scene.get("data"), dict):
        scene = scene["data"]
    # An element list is what makes a JSON object a board. Requiring it keeps
    # other JSON from reading as an empty one. Such files do exist under this
    # MIME type: Nextcloud's older stock templates are in the Spacedeck format.
    if not isinstance(scene, dict) or not isinstance(scene.get("elements"), list):
        raise InvalidBoardError("not in the Excalidraw format (no element list)")
    return scene


def summarize_board(scene: dict[str, Any]) -> list[WhiteboardElement]:
    """The visible elements of a scene from :func:`parse_board`, back to front.

    Deleted elements are dropped: the app keeps them as tombstones. A label is
    a separate text element in the file; here it becomes the ``text`` of the
    shape or arrow that holds it.
    """
    live = [
        element
        for element in scene["elements"]
        if isinstance(element, dict)
        and _identifier(element.get("id"))
        and _identifier(element.get("type"))
        and element.get("isDeleted") is not True
    ]
    # A text can hold nothing, so it is never a container. This also keeps a
    # text that names itself, or two that name each other, from disappearing.
    containers = {_identifier(e["id"]) for e in live if e["type"] != "text"}

    labels: dict[str, list[str]] = {}
    standalone = []
    for element in live:
        container = _identifier(element.get("containerId"))
        if element["type"] == "text" and container and container in containers:
            if text := _element_text(element):
                labels.setdefault(container, []).append(text)
        else:
            standalone.append(element)

    return [
        _summarize(e, labels.get(_identifier(e["id"]) or "", [])) for e in standalone
    ]


def _summarize(element: dict[str, Any], labels: list[str]) -> WhiteboardElement:
    custom = element.get("customData")
    custom = custom if isinstance(custom, dict) else {}

    element_type = _identifier(element["type"]) or ""
    texts = [_element_text(element), *labels]
    # Whiteboard stores both of these as a stand-in: a table is an image whose
    # HTML source is in customData, a comment an invisible 1x1 ellipse.
    if custom.get("isTable") is True:
        element_type = "table"
        texts.append(_table_text(custom.get("tableHtml")))
    elif custom.get("type") == "comment":
        element_type = "comment"
        texts.append(_comment_text(custom.get("commentThread")))

    groups = element.get("groupIds")
    groups = groups[:MAX_GROUPS] if isinstance(groups, list) else []
    group_ids = [_identifier(group) for group in groups]
    text = "\n".join(text for text in texts if text)
    link = _string(element.get("link"))
    return WhiteboardElement(
        id=_identifier(element["id"]) or "",
        type=element_type,
        text=text[:MAX_TEXT_LENGTH] or None,
        text_truncated=len(text) > MAX_TEXT_LENGTH or None,
        x=_rounded(element.get("x")),
        y=_rounded(element.get("y")),
        width=_rounded(element.get("width")),
        height=_rounded(element.get("height")),
        start=_bound_id(element.get("startBinding")),
        end=_bound_id(element.get("endBinding")),
        frame=_identifier(element.get("frameId")),
        groups=[group_id for group_id in group_ids if group_id] or None,
        link=link if link and len(link) <= MAX_LINK_LENGTH else None,
    )


def _string(value: Any) -> str | None:
    """A non-empty string, or None for anything else.

    JSON can escape half of a surrogate pair, which Python accepts and then
    cannot encode, so it is replaced here rather than failing the response.
    """
    if not isinstance(value, str) or not value:
        return None
    return value.encode("utf-8", "replace").decode("utf-8")


def _identifier(value: Any) -> str | None:
    text = _string(value)
    return text if text and len(text) <= MAX_ID_LENGTH else None


def _rounded(value: Any) -> int | None:
    # bool is an int in Python, and json.loads accepts Infinity and NaN.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return round(value)


def _bound_id(binding: Any) -> str | None:
    return _identifier(binding.get("elementId")) if isinstance(binding, dict) else None


def _element_text(element: dict[str, Any]) -> str | None:
    if element["type"] in ("frame", "magicframe"):
        return _string(element.get("name"))
    # originalText is what was typed; text has the line breaks that wrapping
    # it into its container added.
    return _string(element.get("originalText")) or _string(element.get("text"))


def _comment_text(thread: Any) -> str | None:
    comments = thread.get("comments") if isinstance(thread, dict) else None
    lines = []
    for comment in comments if isinstance(comments, list) else []:
        if isinstance(comment, dict) and (text := _string(comment.get("text"))):
            author = _string(comment.get("author"))
            lines.append(f"{author}: {text}" if author else text)
    return "\n".join(lines) or None


def _table_text(html: Any) -> str | None:
    source = _string(html)
    if source is None:
        return None
    parser = _TableText()
    parser.feed(source)
    parser.close()
    rows = [" | ".join(" ".join(c.split()) for c in row) for row in parser.rows if row]
    return "\n".join(rows) or None


class _TableText(HTMLParser):
    """Collects the cell texts of an HTML table, row by row."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows.append([])
            self._in_cell = False
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            self.rows[-1].append("")
            self._in_cell = True
        elif self._in_cell and tag in ("p", "br", "li", "div"):
            # These separate words that would otherwise run together.
            self.rows[-1][-1] += " "

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th", "tr", "table"):
            self._in_cell = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self.rows[-1][-1] += data
