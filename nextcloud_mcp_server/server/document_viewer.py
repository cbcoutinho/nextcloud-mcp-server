"""MCP Apps document viewer.

Tools bound to ``ui://nextcloud-mcp/document-viewer.html``: open a file with its
indexed chunk geometry (optionally staging proposed redactions), and, app-only,
read its bytes, preview a redaction in memory, and save it as a NEW copy
(etag-guarded, create-only; the original is never changed). Geometry comes from the
Qdrant payload (``chunk_bbox``, ``page_number``), never from server-side
rasterising.
"""

import base64
import logging
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import anyio
import pymupdf
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import ToolAnnotations
from mcp_types import CallToolResult, TextContent
from nextcloud_client.webdav import normalize_etag
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.document_processors._native_locks import (
    pymupdf_serialized,
)
from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

VIEWER_URI = "ui://nextcloud-mcp/document-viewer.html"
_VIEWER_FILE = Path(__file__).with_name("document_viewer.html")
MAX_BYTES = 25 * 1024 * 1024
MAX_CHUNKS = 1000  # shortcut: one scroll page; paginate if docs exceed this

apps = Apps()


class RedactTerm(BaseModel):
    text: str = Field(description="Exact text as it appears in the document")
    group: str | None = Field(
        default=None,
        max_length=60,
        description=(
            "Label to group this text under for review: a person ('Mother', "
            "'Person A'), a data type ('Phone numbers', 'Addresses') or a section, "
            "whichever fits the user's request."
        ),
    )


class ReviewButton(BaseModel):
    """One review shortcut; it only flips the viewer's group toggles."""

    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=40)
    set: Literal["on", "off", "only"] = Field(
        description="'only' turns these groups on and every other group off"
    )
    groups: list[Annotated[str, Field(max_length=60)]] = Field(
        min_length=1, max_length=20, description="Group names as used in redact_text"
    )


class Review(BaseModel):
    """Model-described review controls; the viewer renders them as plain text."""

    model_config = ConfigDict(extra="forbid")
    heading: str | None = Field(default=None, max_length=60)
    buttons: list[ReviewButton] = Field(default_factory=list, max_length=6)


class Region(BaseModel):
    page: int  # 1-based
    bbox: tuple[float, float, float, float]  # normalised 0..1, top-left origin


async def _resolve(ctx: Context, file_id: int) -> tuple[Any, dict[str, Any]]:
    """The client plus the file's row; raises if the user can't reach it."""
    client = await get_client(ctx)
    rows = await client.webdav.find_files_by_ids([file_id])
    if not rows:
        raise ValueError(f"File {file_id} not found or not accessible")
    return client, rows[0]


def _result(text: str, structured: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
    )


@apps.tool(
    resource_uri=VIEWER_URI,
    title="Open Document",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
@require_scopes("files.read")
async def open_document(
    ctx: Context,
    file_id: int,
    chunk_index: int | None = None,
    redact_text: list[str | RedactTerm] | None = None,
    review: Review | None = None,
) -> CallToolResult:
    """Open an indexed file in the document viewer with its chunk layout.

    Returns title, path, page count and one line per indexed chunk
    tagged with its chunk index and page, e.g. "[c3 p2] Some text".
    ``chunk_index`` marks the chunk to focus.
    ``redact_text``: exact strings to propose for redaction (e.g. an ID number).
    Every occurrence is staged in the viewer for the user to review and accept.
    Matching is exact and case-insensitive, so prefer full distinctive phrases
    ("Jane Doe", "1 Example Street") over short common ones ("the school").
    Pass items as ``{"text": ..., "group": ...}``. ``group`` is the label to
    review the text under: a person ("Mother", "Person A"), a data type ("Phone
    numbers", "Addresses") or a section, whichever fits the user's request.
    The user accepts or rejects each group together.
    ``review``: use it to shape the review controls when the user asks for a
    particular view, e.g. buttons such as "Keep only Person A" or "Turn off all
    phone numbers". Omit it otherwise.
    The user saves a redacted copy. The original is never changed.
    """
    client, row = await _resolve(ctx, file_id)
    items = [RedactTerm(text=t) if isinstance(t, str) else t for t in redact_text or []]
    groups: dict[str, str] = {}
    for it in items:
        if it.group:
            groups.setdefault(it.text, it.group)
    texts = [it.text for it in items]
    staged: list[dict[str, Any]] = []
    if texts:
        data, _, etag = await client.webdav.read_file(row["path"])
        if len(data) > MAX_BYTES:
            raise ValueError(f"File is {len(data)} bytes; viewer cap is {MAX_BYTES}")
        terms = [t for t in texts if len(t.strip()) >= MIN_TERM_CHARS]
        staged = await anyio.to_thread.run_sync(_find_text, data, terms)
        hits = Counter(r["term"] for r in staged)
        too_broad = {t for t, n in hits.items() if n > MAX_TERM_REGIONS}
        staged = [r for r in staged if r["term"] not in too_broad]
        for r in staged:
            if r["term"] in groups:
                r["group"] = groups[r["term"]]
    else:
        info = await client.webdav.get_file_info(row["path"]) or {}
        etag = normalize_etag(info.get("etag"))
    points, _ = await (await get_qdrant_client()).scroll(
        collection_name=get_settings().get_collection_name(),
        scroll_filter=Filter(
            must=[
                get_placeholder_filter(),
                FieldCondition(key="doc_type", match=MatchValue(value="file")),
                FieldCondition(key="doc_id", match=MatchValue(value=str(file_id))),
            ]
        ),
        limit=MAX_CHUNKS,
        with_vectors=False,
        with_payload=[
            "chunk_index",
            "page_number",
            "page_end",
            "page_count",
            "chunk_bbox",
            "excerpt",
            "title",
        ],
    )
    # Access was proven by _resolve; a deduplicated shared file can carry one
    # point per owner for the same chunk, so keep the first per chunk_index.
    by_index: dict[int, dict[str, Any]] = {}
    for p in points:
        payload = p.payload or {}
        by_index.setdefault(payload.get("chunk_index", -1), payload)

    first = next(iter(by_index.values()), {})
    path = row["path"]
    title = first.get("title") or PurePosixPath(path).name
    chunks = [
        {
            "index": i,
            "page": pl.get("page_number"),
            "page_end": pl.get("page_end"),
            "bboxes": pl.get("chunk_bbox") or [],
            "text": pl.get("excerpt") or "",
        }
        for i, pl in sorted(by_index.items())
    ]
    structured = {
        "file_id": file_id,
        "path": path,
        "title": title,
        "mime_type": row.get("content_type") or "",
        "page_count": first.get("page_count"),
        "focus_chunk": chunk_index,
        "chunks": chunks,
        "etag": etag,
        "staged_regions": staged,
        "review": review.model_dump(exclude_none=True) if review else None,
    }
    lines = [
        f"{title}",
        f"Path: {path}",
        f"Pages: {structured['page_count'] or 'unknown'}",
        f"{len(chunks)} indexed chunks" + ("" if chunks else " (file not indexed yet)"),
    ]
    if texts:
        short = [t for t in texts if len(t.strip()) < MIN_TERM_CHARS]
        missing = [t for t in terms if t not in hits]
        lines.append(
            f"Staged {len(staged)} region(s) for redaction in the viewer "
            f"({len(terms) - len(missing) - len(too_broad)} of {len(texts)} "
            "term(s) staged); the user must review and save them there."
        )
        lines.append(
            "Regions per term (case-insensitive substring match): "
            + "; ".join(f"{t!r}: {hits[t]}" for t in terms if t in hits)
        )
        broad = [t for t in terms if WARN_TERM_REGIONS < hits[t] <= MAX_TERM_REGIONS]
        if broad:
            lines.append(
                f"Broad terms (over {WARN_TERM_REGIONS} regions; likely matching more "
                "than intended, consider a longer phrase): "
                + "; ".join(repr(t) for t in broad)
            )
        if short:
            lines.append(
                f"Skipped (shorter than {MIN_TERM_CHARS} characters): "
                + "; ".join(repr(t) for t in short)
            )
        if too_broad:
            lines.append(
                f"Not staged, too broad (over {MAX_TERM_REGIONS} regions each; use a "
                "longer, more specific phrase): "
                + "; ".join(repr(t) for t in sorted(too_broad))
            )
        if missing:
            lines.append(
                "Not found in the document text (pass the exact text as it "
                "appears in the chunks below, not a description): "
                + "; ".join(repr(t) for t in missing)
            )
    lines += [
        f"[c{c['index']} p{c['page']}] {' '.join(str(c['text']).split())[:120]}"
        for c in chunks
    ]
    return _result("\n".join(lines), structured)


@apps.tool(
    resource_uri=VIEWER_URI,
    visibility=["app"],
    title="Read Document Bytes",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
@require_scopes("files.read")
async def read_document_bytes(ctx: Context, file_id: int) -> CallToolResult:
    """Return a file's raw bytes (base64) for the viewer to render."""
    client, row = await _resolve(ctx, file_id)
    data, mime_type, _ = await client.webdav.read_file(row["path"])
    if len(data) > MAX_BYTES:
        raise ValueError(f"File is {len(data)} bytes; viewer cap is {MAX_BYTES}")
    return _result(
        f"{row['path']}: {len(data)} bytes ({mime_type})",
        {
            "mime_type": mime_type,
            "size": len(data),
            "data_b64": base64.b64encode(data).decode(),
        },
    )


def _find_text(pdf: bytes, needles: list[str]) -> list[dict[str, Any]]:
    """Every hit of each needle as a normalised region (the model-proposes path)."""
    found = []
    with pymupdf_serialized(), pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for page in doc:
            w, h = page.rect.width, page.rect.height
            for needle in needles:
                for r in page.search_for(needle):
                    found.append(
                        {
                            "page": page.number + 1,
                            "bbox": [r.x0 / w, r.y0 / h, r.x1 / w, r.y1 / h],
                            "term": needle,
                        }
                    )
    return found


def _redact(pdf: bytes, regions: list[Region]) -> bytes:
    with pymupdf_serialized(), pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for r in regions:
            if not 1 <= r.page <= doc.page_count:
                raise ValueError(f"Page {r.page} out of range 1..{doc.page_count}")
            page = doc[r.page - 1]
            w, h = page.rect.width, page.rect.height
            x0, y0, x1, y1 = r.bbox
            # shortcut: ignores /Rotate; derotate via page.derotation_matrix if
            # rotated scans show misplaced boxes.
            page.add_redact_annot(
                pymupdf.Rect(x0 * w, y0 * h, x1 * w, y1 * h), fill=(0, 0, 0)
            )
        for page in doc:
            page.apply_redactions()
        return doc.tobytes(garbage=3, deflate=True)


async def _redacted(
    ctx: Context, file_id: int, regions: list[Region]
) -> tuple[Any, dict[str, Any], bytes, str | None]:
    """Download the PDF and apply the redactions in memory; nothing is written."""
    if not regions:
        raise ValueError("No regions to redact")
    client, row = await _resolve(ctx, file_id)
    data, _, etag = await client.webdav.read_file(row["path"])
    if len(data) > MAX_BYTES:
        raise ValueError(f"File is {len(data)} bytes; redaction cap is {MAX_BYTES}")
    return client, row, await anyio.to_thread.run_sync(_redact, data, regions), etag


@apps.tool(
    resource_uri=VIEWER_URI,
    visibility=["app"],
    title="Preview Redaction",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
@require_scopes("files.read")
async def preview_redaction(
    ctx: Context, file_id: int, regions: list[Region]
) -> CallToolResult:
    """Return the PDF with ``regions`` redacted (base64), without saving it."""
    _, row, out, etag = await _redacted(ctx, file_id, regions)
    return _result(
        f"Preview of {row['path']} with {len(regions)} region(s) redacted.",
        {
            "mime_type": "application/pdf",
            "size": len(out),
            "data_b64": base64.b64encode(out).decode(),
            "etag": etag,
        },
    )


# A term matching more than this is almost certainly a fragment ("D", "mother").
MAX_TERM_REGIONS = 100
WARN_TERM_REGIONS = 25
MIN_TERM_CHARS = 3
MAX_COPY_SUFFIX = 100  # shortcut: linear -vN probe; list the folder if it grows


def _default_name(src: str, n: int = 1) -> str:
    """``<stem>-redacted.pdf``, then ``-redacted-v2.pdf``… (mirrors SAR's -vN)."""
    stem = PurePosixPath(src).stem
    return f"{stem}-redacted.pdf" if n == 1 else f"{stem}-redacted-v{n}.pdf"


def _target_path(src: str, name: str) -> str:
    """An explicit copy name: bare names land beside ``src``; paths are as given."""
    name = name.strip()
    parts = PurePosixPath(name).parts
    if not name or name.endswith("/") or "\\" in name or ".." in parts:
        raise ValueError(f"Invalid name {name!r}")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name if "/" in name else str(PurePosixPath(src).parent / name)


class SaveConfirmation(BaseModel):
    confirm: bool = Field(description="Save a redacted copy (the original is kept)")
    name: str = Field(default="", description="File name for the copy (empty: default)")


async def _confirm_save(
    ctx: Context, file_id: int, name: str | None = None
) -> SaveConfirmation | Elicit[SaveConfirmation]:
    """Ask elicitation-capable hosts to confirm; others confirmed in their own UI.

    The framework renders Elicit as an InputRequiredResult (2026-07-28 MRTR) or
    a standalone elicitation/create on older revisions.
    """
    caps = ctx.client_capabilities
    if caps is None or caps.elicitation is None:
        return SaveConfirmation(confirm=True, name=name or "")
    _, row = await _resolve(ctx, file_id)
    suggested = name or _default_name(row["path"])

    class Ask(SaveConfirmation):  # elicitation schemas need a plain-str default
        name: str = Field(default=suggested, description="File name for the copy")

    return Elicit(
        f"Save a redacted copy of {PurePosixPath(row['path']).name}? "
        "The original is not changed.",
        Ask,
    )


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


@apps.tool(
    resource_uri=VIEWER_URI,
    visibility=["app"],
    title="Save Redacted Copy",
    annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
)
@require_scopes("files.read", "files.write")
async def save_redaction(
    ctx: Context,
    file_id: int,
    regions: list[Region],
    etag: str,
    confirmation: Annotated[
        ElicitationResult[SaveConfirmation], Resolve(_confirm_save)
    ],
    name: str | None = None,
) -> CallToolResult:
    """Save ``regions`` redacted as a NEW PDF. The original is never changed.

    The source must still be at ``etag`` (the version the user reviewed). With no
    ``name`` the copy is ``<stem>-redacted.pdf`` beside the original, stepping to
    ``-redacted-v2.pdf``… if taken. An explicit ``name`` (bare file name, or a
    path) must not exist yet.
    """
    if not isinstance(confirmation, AcceptedElicitation):
        return _error(f"Redaction not saved: {confirmation.action}.")
    choice = confirmation.data
    if not choice.confirm:
        return _error("Redaction not saved: not confirmed.")
    client, row, out, current = await _redacted(ctx, file_id, regions)
    if current is not None and normalize_etag(etag) != current:
        return _error("conflict: file changed since it was opened")

    src = row["path"]
    # The elicitation form's unedited suggestion (no ``name`` was passed) is the
    # default, so it still gets -vN; a name the caller passed is explicit.
    explicit = choice.name or None
    if name is None and explicit == _default_name(src):
        explicit = None
    try:
        candidates = (
            [_target_path(src, explicit)]
            if explicit
            else [
                str(PurePosixPath(src).parent / _default_name(src, n))
                for n in range(1, MAX_COPY_SUFFIX + 1)
            ]
        )
    except ValueError as e:
        return _error(str(e))
    for path in candidates:
        # if_match=None sends If-None-Match: * -- create-only, never overwrites.
        written = await client.webdav.write_file(
            path, out, content_type="application/pdf"
        )
        if written.get("status_code") != 412:
            break
        if explicit:
            return _error(f"{path} already exists; choose another name")
    else:
        return _error(f"No free name for a redacted copy of {src}")
    if "message" in written:
        return _error(written["message"])

    new_id = await client.webdav.get_fileid(path)
    return _result(
        f"Saved a redacted copy ({len(regions)} region(s)) as {path}; "
        f"the original {src} is unchanged.",
        {
            "file_id": int(new_id) if new_id else None,
            "source_file_id": file_id,
            "path": path,
            "redacted_regions": len(regions),
        },
    )


def _viewer_html() -> str:
    try:
        return _VIEWER_FILE.read_text()
    except FileNotFoundError:
        logger.warning("%s missing; serving a placeholder viewer", _VIEWER_FILE)
        return "<!doctype html><p>Document viewer not installed.</p>"


apps.add_html_resource(
    VIEWER_URI,
    _viewer_html(),
    title="Document viewer",
    # pdf.js 3.11.174 is the viewer's only external load, pinned with SRI hashes.
    csp=ResourceCsp(resource_domains=["https://cdnjs.cloudflare.com"]),
)
