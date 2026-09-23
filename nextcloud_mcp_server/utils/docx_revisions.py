"""Add native track-changes revisions (``<w:ins>``/``<w:del>``) to a .docx.

A suggested edit has to live *inside* the document to be reviewable: Word,
LibreOffice, Collabora and OnlyOffice all render ``<w:ins>``/``<w:del>`` as
tracked changes a reviewer can accept or reject, whereas a Nextcloud file
comment is invisible in every editor. This module produces exactly those
elements and nothing else.

It edits the main document part at the XML level with ``lxml`` rather than
round-tripping through ``python-docx``: every other part of the package
(styles, media, comments, custom XML) is copied through byte for byte, so the
only difference between input and output is the revision itself.

Matching is per paragraph, over the text of the runs that are direct children
of ``<w:p>``. Anything else in the paragraph that carries text -- a hyperlink,
an existing tracked change, a content control, a field -- acts as a barrier a
match cannot cross, so the runs a match covers can always be wrapped without
restructuring their surroundings. Pure bookkeeping siblings (``w:proofErr``,
bookmarks, comment ranges) are transparent: Word sprinkles them between runs
mid-word, and treating them as barriers would make ordinary anchors unmatchable.
"""

from __future__ import annotations

import copy
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from lxml import etree  # type: ignore[import-untyped]  # ty: ignore[unresolved-import]

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

RevisionMode = Literal["insert", "delete", "replace"]

#: Stands in for run content that is not text (a drawing, a field character, a
#: footnote reference). Not something a caller can type into ``anchor_text``,
#: so a match can never straddle it.
_OPAQUE = "\ufffc"

#: Run children whose text contribution is known. Anything else is opaque.
_TEXT_EQUIVALENTS = {
    "tab": "\t",
    "br": "\n",
    "cr": "\n",
    "noBreakHyphen": "\u2011",
    "softHyphen": "\u00ad",
}


#: Characters XML 1.0 cannot carry at all (not even escaped). lxml raises a bare
#: ValueError on them, so they are refused up front with a message that names
#: the offending argument.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


class DocxRevisionError(ValueError):
    """The requested revision cannot be applied; the message says why."""


@dataclass(frozen=True)
class RevisionResult:
    """What was changed, for the tool response."""

    content: bytes
    revision_ids: list[int]
    paragraph_text: str
    match_count: int


@dataclass
class _Segment:
    """One single-content run, and where its text sits in the paragraph."""

    run: etree._Element
    start: int
    end: int


def _main_part_name(archive: zipfile.ZipFile) -> str:
    """Resolve the main document part from the package relationships.

    Almost always ``word/document.xml``, but the package root rels are what
    actually say so, and nothing requires the conventional name.
    """
    try:
        rels = etree.fromstring(archive.read("_rels/.rels"))
    except KeyError:
        raise DocxRevisionError(
            "Not a Word document: the package has no _rels/.rels"
        ) from None
    for rel in rels.iter(f"{{{_REL_NS}}}Relationship"):
        if rel.get("Type") == _OFFICE_DOCUMENT_REL:
            return posixpath.normpath(rel.get("Target", "").lstrip("/"))
    raise DocxRevisionError("Not a Word document: no officeDocument relationship")


def _split_run_children(paragraph: etree._Element) -> None:
    """Rewrite each direct-child run so it holds at most one content element.

    ``<w:r><w:rPr/><w:t>a</w:t><w:tab/><w:t>b</w:t></w:r>`` becomes three runs
    sharing the same ``rPr``. That renders identically and means every later
    split or wrap works on whole runs only.
    """
    # findall() snapshots the runs: the loop adds and removes siblings.
    for run in paragraph.findall(_w("r")):
        rpr = run.find(_w("rPr"))
        content = [c for c in run if c is not rpr]
        if len(content) <= 1:
            continue
        anchor = run
        for child in content:
            new_run = etree.Element(_w("r"), attrib=dict(run.attrib))
            if rpr is not None:
                new_run.append(copy.deepcopy(rpr))
            new_run.append(child)
            anchor.addnext(new_run)
            anchor = new_run
        paragraph.remove(run)


def _run_text(run: etree._Element) -> str:
    for child in run:
        if child.tag == _w("rPr"):
            continue
        if child.tag == _w("t"):
            return child.text or ""
        local = etree.QName(child).localname
        if child.tag == _w(local) and local in _TEXT_EQUIVALENTS:
            return _TEXT_EQUIVALENTS[local]
        return _OPAQUE
    return ""


def _index_paragraph(paragraph: etree._Element) -> tuple[str, list[_Segment]]:
    """Return the matchable text of a paragraph and the runs that make it up."""
    text_parts: list[str] = []
    segments: list[_Segment] = []
    pos = 0
    for child in paragraph:
        if child.tag == _w("r"):
            piece = _run_text(child)
            segments.append(_Segment(child, pos, pos + len(piece)))
        elif any(True for _ in child.iter(_w("t"), _w("delText"))):
            piece = _OPAQUE
        else:
            continue
        text_parts.append(piece)
        pos += len(piece)
    return "".join(text_parts), segments


def _split_text_run(run: etree._Element, offset: int) -> etree._Element:
    """Split a ``w:t`` run at ``offset``; return the run holding the tail."""
    text_el = run.find(_w("t"))
    if text_el is None:
        raise DocxRevisionError("Internal error: cannot split a non-text run")
    text = text_el.text or ""
    tail = copy.deepcopy(run)
    _set_text(text_el, text[:offset])
    _set_text(tail.find(_w("t")), text[offset:])
    run.addnext(tail)
    return tail


def _set_text(text_el: etree._Element | None, value: str) -> None:
    if text_el is None:
        return
    text_el.text = value
    # Leading/trailing whitespace is dropped by every consumer unless marked.
    if value != value.strip():
        text_el.set(_XML_SPACE, "preserve")


def _runs_covering(
    paragraph: etree._Element, start: int, end: int
) -> list[etree._Element]:
    """Split runs so ``[start, end)`` falls on run boundaries; return those runs."""
    for boundary in (start, end):
        _, segments = _index_paragraph(paragraph)
        for seg in segments:
            if seg.start < boundary < seg.end:
                _split_text_run(seg.run, boundary - seg.start)
                break
    _, segments = _index_paragraph(paragraph)
    return [
        s.run for s in segments if start <= s.start and s.end <= end and s.end > s.start
    ]


def _next_revision_id(root: etree._Element) -> int:
    """First ``w:id`` above every annotation id already in the part.

    Revision, comment and bookmark ids are compared loosely by consumers, so
    staying clear of all of them is the safe choice, not just of other
    revisions.
    """
    highest = -1
    for el in root.iter():
        raw = el.get(_w("id"))
        if raw is not None and raw.lstrip("-").isdigit():
            highest = max(highest, int(raw))
    return highest + 1


def _revision_element(tag: str, rev_id: int, author: str, date: str) -> etree._Element:
    return etree.Element(
        _w(tag),
        attrib={_w("id"): str(rev_id), _w("author"): author, _w("date"): date},
    )


def _mark_deleted(run: etree._Element) -> None:
    """Deleted runs carry their text in ``w:delText``, not ``w:t``."""
    for text_el in run.iterchildren(_w("t")):
        text_el.tag = _w("delText")
    for instr in run.iterchildren(_w("instrText")):
        instr.tag = _w("delInstrText")


def _template_rpr(template: etree._Element | None) -> etree._Element | None:
    """Copy of ``template``'s run properties, minus any formatting revision."""
    rpr = template.find(_w("rPr")) if template is not None else None
    if rpr is None:
        return None
    rpr = copy.deepcopy(rpr)
    # A formatting revision on the template is not part of this change.
    for change in rpr.findall(_w("rPrChange")):
        rpr.remove(change)
    return rpr


def _append_line(run: etree._Element, line: str) -> None:
    """Append one line of text, a tab becoming ``w:tab`` (w:t renders a space)."""
    for j, chunk in enumerate(line.split("\t")):
        if j:
            etree.SubElement(run, _w("tab"))
        if chunk:
            _set_text(etree.SubElement(run, _w("t")), chunk)


def _inserted_run(template: etree._Element | None, text: str) -> etree._Element:
    """A run carrying ``text`` in the formatting of ``template`` (if any)."""
    run = etree.Element(_w("r"))
    rpr = _template_rpr(template)
    if rpr is not None:
        run.append(rpr)
    # A newline in new_text is a line break: w:t would render it as a space.
    for i, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if i:
            etree.SubElement(run, _w("br"))
        _append_line(run, line)
    return run


def _validate_arguments(
    anchor_text: str,
    new_text: str,
    mode: RevisionMode,
    author: str,
    occurrence: int | None,
) -> None:
    if not anchor_text:
        raise DocxRevisionError("anchor_text must not be empty")
    if _OPAQUE in anchor_text:
        raise DocxRevisionError("anchor_text must not contain U+FFFC")
    if mode == "delete" and new_text:
        raise DocxRevisionError("new_text must be empty when mode is 'delete'")
    if mode in ("insert", "replace") and not new_text:
        raise DocxRevisionError(f"new_text is required when mode is {mode!r}")
    for name, value in (("new_text", new_text), ("author", author)):
        if _XML_ILLEGAL.search(value):
            raise DocxRevisionError(
                f"{name} contains a control character a .docx cannot store"
            )
    if occurrence is not None and occurrence < 1:
        raise DocxRevisionError("occurrence is 1-based and must be >= 1")


def _open_package(docx: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(docx))
    except zipfile.BadZipFile:
        raise DocxRevisionError(
            "Not a Word document: the file is not a zip package"
        ) from None


def _load_main_part(archive: zipfile.ZipFile) -> tuple[str, etree._Element]:
    """Return the main document part's name and parsed root."""
    part_name = _main_part_name(archive)
    try:
        root = etree.fromstring(archive.read(part_name))
    except KeyError:
        raise DocxRevisionError(
            f"Main document part {part_name!r} is missing"
        ) from None
    if root.tag != _w("document"):
        raise DocxRevisionError("Not a WordprocessingML document")
    return part_name, root


def _find_matches(
    root: etree._Element, anchor_text: str
) -> list[tuple[etree._Element, str, int]]:
    """Every (paragraph, paragraph text, offset) where ``anchor_text`` occurs."""
    matches: list[tuple[etree._Element, str, int]] = []
    for paragraph in root.iter(_w("p")):
        text, _ = _index_paragraph(paragraph)
        start = text.find(anchor_text)
        while start != -1:
            matches.append((paragraph, text, start))
            start = text.find(anchor_text, start + 1)
    return matches


def _select_match(
    matches: list[tuple[etree._Element, str, int]],
    anchor_text: str,
    occurrence: int | None,
) -> tuple[etree._Element, str, int]:
    """Pick the targeted match, refusing a missing or ambiguous anchor."""
    if not matches:
        raise DocxRevisionError(
            f"anchor_text {anchor_text!r} was not found in any paragraph. It "
            "must match the document text exactly and lie within a single "
            "paragraph, outside hyperlinks and existing tracked changes."
        )
    if occurrence is None and len(matches) > 1:
        raise DocxRevisionError(
            f"anchor_text {anchor_text!r} occurs {len(matches)} times; pass "
            "occurrence (1-based) or a longer, unique anchor"
        )
    index = (occurrence or 1) - 1
    if index >= len(matches):
        raise DocxRevisionError(
            f"occurrence={occurrence} requested but anchor_text occurs only "
            f"{len(matches)} time(s)"
        )
    return matches[index]


def _wrap_in_deletion(covered: list[etree._Element], deletion: etree._Element) -> None:
    """Move the covered span into ``deletion``, placed where the span was.

    Everything from the first to the last covered run moves, bookkeeping
    siblings (proofErr, bookmarks) included, so document order is preserved. No
    barrier can sit in that span: a match never crosses one.
    """
    span = [covered[0]]
    while span[-1] is not covered[-1]:
        span.append(span[-1].getnext())
    covered[0].addprevious(deletion)
    for el in span:
        if el.tag == _w("r"):
            _mark_deleted(el)
        deletion.append(el)


def _repackage(archive: zipfile.ZipFile, part_name: str, root: etree._Element) -> bytes:
    """The package with ``part_name`` replaced by ``root``, all else copied as is."""
    new_part = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as rewritten:
        for info in archive.infolist():
            data = new_part if info.filename == part_name else archive.read(info)
            rewritten.writestr(info, data, compress_type=info.compress_type)
    return out.getvalue()


def apply_revision(
    docx: bytes,
    anchor_text: str,
    new_text: str = "",
    *,
    mode: RevisionMode = "insert",
    author: str,
    occurrence: int | None = None,
    now: datetime | None = None,
) -> RevisionResult:
    """Apply one tracked change to ``docx`` and return the rewritten package.

    Args:
        docx: The .docx file.
        anchor_text: Text to locate, matched exactly within a single paragraph
            (it may span several differently formatted runs).
        new_text: The suggested text: inserted after the anchor (``insert``) or
            in place of it (``replace``). Must be empty for ``delete``.
        mode: ``insert`` adds ``new_text`` after the anchor; ``delete`` marks
            the anchor itself as deleted; ``replace`` does both.
        author: Author recorded on the revision.
        occurrence: 1-based occurrence of the anchor to target. Required when
            the anchor occurs more than once, so an ambiguous anchor is never
            guessed at.
        now: Revision timestamp (UTC now if omitted; injectable for tests).

    Raises:
        DocxRevisionError: When the file is not a Word document or the anchor
            cannot be resolved to exactly one location.
    """
    _validate_arguments(anchor_text, new_text, mode, author, occurrence)

    with _open_package(docx) as archive:
        part_name, root = _load_main_part(archive)
        matches = _find_matches(root, anchor_text)
        paragraph, paragraph_text, start = _select_match(
            matches, anchor_text, occurrence
        )

        _split_run_children(paragraph)
        covered = _runs_covering(paragraph, start, start + len(anchor_text))
        if not covered:  # pragma: no cover - a non-empty match covers >= 1 run
            raise DocxRevisionError("Internal error: the match covers no run")

        date = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rev_id = _next_revision_id(root)
        revision_ids: list[int] = []
        last: etree._Element = covered[-1]

        if mode in ("delete", "replace"):
            last = _revision_element("del", rev_id, author, date)
            _wrap_in_deletion(covered, last)
            revision_ids.append(rev_id)
            rev_id += 1

        if mode in ("insert", "replace"):
            insertion = _revision_element("ins", rev_id, author, date)
            insertion.append(_inserted_run(covered[-1], new_text))
            last.addnext(insertion)
            revision_ids.append(rev_id)

        content = _repackage(archive, part_name, root)

    return RevisionResult(
        content=content,
        revision_ids=revision_ids,
        paragraph_text=paragraph_text.replace(_OPAQUE, ""),
        match_count=len(matches),
    )
