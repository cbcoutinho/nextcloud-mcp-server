"""Redacted export archives for subject access requests (ADR-040).

An export takes the documents an operator selected, redacts every third party
(names, emails, phone numbers, NI numbers) while keeping the data subject's own
identifiers, renders each document to PDF and writes one zip to an output folder
in Nextcloud:

    <output folder>/<name>.zip           index.pdf, documents/NNN-<title>.pdf, searches.pdf
    <output folder>/<name>.status.json   progress; ids and counts only, never content

Document text comes from the search index, which stores every chunk's full text
and character offsets. Reassembling it needs no re-parse or OCR, and it is exactly
the text the operator searched.
"""

import html
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pymupdf
from anyio.abc import TaskGroup
from httpx import HTTPStatusError
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.models.sar import (
    SarExportRequest,
    SarExportStatus,
    SarFailedItem,
    SarItem,
)
from nextcloud_mcp_server.providers.ner import NerClient
from nextcloud_mcp_server.redaction import (
    Redactor,
    counts,
    detect_names,
    get_ner_client,
)
from nextcloud_mcp_server.search.access_filter import (
    build_ownership_filter,
    list_accessible_owners,
)
from nextcloud_mcp_server.vector.oauth_sync import (
    NotProvisionedError,
    resolve_background_client,
)
from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

# An archive name becomes two file names; keep it to one plain path segment.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")
_SCROLL_PAGE = 256
_PAGE_MARGIN = 50  # points


class ExportError(Exception):
    """The export cannot start or finish. The message is safe to show.

    ``status`` is the HTTP status the management API answers with.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _ItemError(Exception):
    """One document cannot be exported. The message goes in the index."""


@dataclass
class _Doc:
    item: SarItem
    title: str = ""
    text: str = ""
    error: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def archive_paths(output_folder: str, name: str) -> tuple[str, str]:
    """``(archive_path, status_path)`` for an export, validating both inputs.

    Both are user-controlled. The user's own credentials bound what can be
    written, but a ``..`` segment is refused outright rather than left to the
    server's path normalisation.
    """
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise ExportError(
            "name must be 1-100 letters, digits, spaces, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    segments = [s for s in output_folder.strip().split("/") if s]
    if any(s in (".", "..") for s in segments):
        raise ExportError("output_folder must not contain '.' or '..' segments")
    folder = "/" + "/".join(segments) if segments else ""
    return f"{folder}/{name}.zip", f"{folder}/{name}.status.json"


# --- Text from the index ---------------------------------------------------


def stitch(chunks: list[tuple[int, str]]) -> str:
    """Reassemble ``(start_offset, text)`` chunks into one text.

    Chunks overlap (the chunker repeats a tail of each in the next) and may have
    gaps (whitespace between pages). Overlap is dropped by offset; a gap becomes
    a line break.
    """
    parts: list[str] = []
    end: int | None = None
    for start, text in sorted(chunks, key=lambda c: c[0]):
        if end is None:
            parts.append(text)
        elif start >= end:
            parts.append(("\n" if start > end else "") + text)
        else:
            parts.append(text[end - start :])
        end = max(end or 0, start + len(text))
    return "".join(parts)


def _in_pages(payload: dict[str, Any], first: int, last: int) -> bool:
    page = payload.get("page_number")
    if page is None:
        return False
    return page <= last and (payload.get("page_end") or page) >= first


async def document_text(
    nc: NextcloudClient, user_id: str, item: SarItem
) -> tuple[str, str]:
    """``(title, text)`` of one document, rebuilt from its indexed chunks.

    Raises:
        _ItemError: the user cannot access it, it is not indexed, or the
            requested pages hold no text.
    """
    owners: list[str] | None = None
    if item.doc_type == "file":
        # Live check: the file still exists and this user can open it. Only
        # then widen the index lookup to owners who share with the user, as
        # search/context.py does for context expansion.
        if not item.doc_id.isdigit() or not await nc.webdav.file_accessible_by_id(
            int(item.doc_id)
        ):
            raise _ItemError("not accessible to the requesting user")
        owners = await list_accessible_owners(nc.sharing, user_id)
    # ponytail: non-file items rely on the index's ownership filter (self-only)
    # without a live existence check; add one per doc type via
    # search/verification.py if deleted notes/cards ever reach an export.

    qdrant = await get_qdrant_client()
    scroll_filter = Filter(
        must=[
            build_ownership_filter(user_id, owners),
            FieldCondition(key="doc_id", match=MatchValue(value=item.doc_id)),
            FieldCondition(key="doc_type", match=MatchValue(value=item.doc_type)),
            get_placeholder_filter(),
        ]
    )
    payloads: dict[int, dict[str, Any]] = {}
    offset = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=get_settings().get_collection_name(),
            scroll_filter=scroll_filter,
            limit=_SCROLL_PAGE,
            offset=offset,
            with_payload=[
                "title",
                "excerpt",
                "chunk_index",
                "chunk_start_offset",
                "page_number",
                "page_end",
            ],
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            # One chunk per index: a shared file can be indexed under more than
            # one owner.
            payloads.setdefault(int(payload.get("chunk_index", 0)), payload)
        if offset is None:
            break
    if not payloads:
        raise _ItemError("not in the search index")

    chunks = list(payloads.values())
    if item.page_start is not None or item.page_end is not None:
        if all(p.get("page_number") is None for p in chunks):
            raise _ItemError("a page range was given but the document has no pages")
        first = item.page_start or 1
        last = item.page_end if item.page_end is not None else 10**9
        chunks = [p for p in chunks if _in_pages(p, first, last)]
        if not chunks:
            raise _ItemError("no indexed text in the requested pages")

    title = str(chunks[0].get("title") or f"{item.doc_type} {item.doc_id}")
    text = stitch(
        [
            (int(p.get("chunk_start_offset") or 0), str(p.get("excerpt") or ""))
            for p in chunks
        ]
    )
    return title, text


# --- Rendering --------------------------------------------------------------


def _pdf(body_html: str) -> bytes:
    """Render an HTML fragment to a multi-page A4 PDF."""
    story = pymupdf.Story(html=body_html)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page = pymupdf.paper_rect("a4")
    where = page + (_PAGE_MARGIN, _PAGE_MARGIN, -_PAGE_MARGIN, -_PAGE_MARGIN)
    more = True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def _e(text: str | None) -> str:
    return html.escape(text or "")


def render_document(title: str, reason: str, text: str) -> bytes:
    return _pdf(
        f"<h2>{_e(title)}</h2>"
        f"<p><i>Reason for inclusion:</i> {_e(reason)}</p><hr/>"
        f"<p style='white-space: pre-wrap; font-size: 10pt'>{_e(text)}</p>"
    )


def render_index(name: str, rows: list[dict[str, Any]]) -> bytes:
    cells = []
    for row in rows:
        detail = (
            f"<b>Not exported:</b> {_e(row['error'])}"
            if row.get("error")
            else f"{_e(row['title'])}<br/><i>{_e(row['reason'])}</i>"
        )
        redacted = ", ".join(f"{k.lower()}: {v}" for k, v in row["counts"].items())
        cells.append(
            f"<tr><td>{row['n']}</td><td>{detail}</td><td>{_e(row['pages'])}</td>"
            f"<td>{_e(redacted) or 'none'}</td></tr>"
        )
    return _pdf(
        f"<h2>{_e(name)}</h2>"
        "<p>Third-party names, email addresses, phone numbers and NI numbers are "
        "replaced with numbered placeholders, consistent across this archive. "
        "Detection is automated and may miss or over-redact; review before "
        "disclosure.</p>"
        "<table border='1' cellpadding='4' style='font-size: 9pt'>"
        "<tr><th>#</th><th>Document</th><th>Pages</th><th>Redacted</th></tr>"
        + "".join(cells)
        + "</table>"
    )


def render_searches(queries: list[str]) -> bytes:
    items = "".join(f"<li>{_e(q)}</li>" for q in queries)
    return _pdf(
        "<h2>Searches</h2>"
        "<p>Queries run to find these documents. The search covered only content "
        "the requesting user can access.</p>"
        f"<ol>{items}</ol>"
    )


def _slug(title: str) -> str:
    return re.sub(r"[^\w\[\]-]+", "-", title).strip("-")[:80] or "document"


def _pages(item: SarItem) -> str:
    if item.page_start is None and item.page_end is None:
        return "all"
    return f"{item.page_start or 1}-{item.page_end or 'end'}"


# --- Job ---------------------------------------------------------------------


async def _write_status(
    nc: NextcloudClient, status: SarExportStatus, *, create: bool = False
) -> None:
    status.updated_at = _now()
    result = await nc.webdav.write_file(
        status.status_path,
        status.model_dump_json(indent=2).encode(),
        "application/json",
        if_match=None if create else "*",
    )
    if result["status_code"] == 412 and create:
        raise ExportError(
            f"an export already exists at {status.status_path}", status=409
        )
    if result["status_code"] in (412, 423):
        raise ExportError(f"could not write {status.status_path}", status=409)


async def read_status(
    nc: NextcloudClient, output_folder: str, name: str
) -> SarExportStatus:
    """The recorded status of an export.

    Raises:
        ExportError: invalid name/folder (400) or no such export (404).
    """
    _, status_path = archive_paths(output_folder, name)
    try:
        content, _, _ = await nc.webdav.read_file(status_path)
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ExportError(
                f"no export named {name!r} in {output_folder!r}", status=404
            ) from e
        raise
    return SarExportStatus.model_validate_json(content)


async def submit_export(
    user_id: str, request: SarExportRequest, task_group: TaskGroup | None
) -> SarExportStatus:
    """Validate, check the output folder and start an export for ``user_id``.

    Shared by the MCP tool and the management API.

    Raises:
        ExportError: with the HTTP status the API should answer with.
    """
    if task_group is None:
        raise ExportError(
            "SAR export is unavailable: background tasks not running", 503
        )
    archive_paths(request.output_folder, request.name)  # validate before any I/O
    try:
        nc = await resolve_background_client(user_id)
    except NotProvisionedError as e:
        raise ExportError(
            "SAR export runs in the background and needs background access: "
            "provision it in Astrolabe's personal settings.",
            status=403,
        ) from e
    try:
        # On success the job owns `nc` and closes it.
        return await start_export(
            nc=nc,
            ner=await get_ner_client(get_settings()),
            task_group=task_group,
            request=request,
        )
    except HTTPStatusError as e:
        await nc.close()
        raise ExportError(
            f"Cannot write to {request.output_folder!r} "
            f"(HTTP {e.response.status_code}): it must exist and be writable.",
            status=403,
        ) from e
    except BaseException:
        await nc.close()
        raise


async def start_export(
    *,
    nc: NextcloudClient,
    ner: NerClient,
    task_group: TaskGroup,
    request: SarExportRequest,
) -> SarExportStatus:
    """Check the output folder, write the initial status, start the job.

    ``nc`` must be a client that outlives the request (see
    ``resolve_background_client``); the job closes it when done.

    Raises:
        ExportError: invalid name, or the name is taken.
        HTTPStatusError: the folder is missing or not writable (403/404/409).
    """
    items = request.items
    archive_path, status_path = archive_paths(request.output_folder, request.name)
    started = _now()
    status = SarExportStatus(
        state="running",
        archive_path=archive_path,
        status_path=status_path,
        total=len(items),
        processed=0,
        failed=0,
        started_at=started,
        updated_at=started,
    )
    # Writing the status file first is the writability check: it fails before
    # any work if the user cannot create files in the folder or the name is
    # taken.
    await _write_status(nc, status, create=True)
    task_group.start_soon(
        _run_and_close,
        nc,
        ner,
        status,
        request.name,
        list(request.subject),
        items,
        list(request.queries),
    )
    return status


async def _run_and_close(
    nc: NextcloudClient,
    ner: NerClient,
    status: SarExportStatus,
    name: str,
    keep: list[str],
    items: list[SarItem],
    queries: list[str],
) -> None:
    try:
        await run_export(nc, ner, status, name, keep, items, queries)
    except Exception as e:
        # Never log or record content: only the exception type.
        logger.exception("SAR export to %s failed", status.archive_path)
        status.state = "failed"
        status.message = (
            str(e)
            if isinstance(e, ExportError)
            else f"export failed ({type(e).__name__})"
        )
        try:
            await _write_status(nc, status)
        except Exception:
            logger.exception("Could not record failure of %s", status.status_path)
    finally:
        await nc.close()


async def run_export(
    nc: NextcloudClient,
    ner: NerClient,
    status: SarExportStatus,
    name: str,
    keep: list[str],
    items: list[SarItem],
    queries: list[str],
) -> None:
    """Build and upload the archive, updating ``status`` as it goes.

    A document that cannot be read is recorded as failed and the export goes
    on. A name-detection failure fails the whole export: it is systemic, and
    nothing may be written unredacted.
    """
    user_id = nc.username
    docs = [_Doc(item) for item in items]

    # Pass 1: read every document and detect names across all of them, so one
    # name set (and one numbering) covers the archive.
    # ponytail: texts are held in memory for the whole export; fine for
    # hundreds of documents, re-read per pass if archives grow far beyond that.
    names: set[str] = set()
    for doc in docs:
        try:
            doc.title, doc.text = await document_text(nc, user_id, doc.item)
        except _ItemError as e:
            doc.error = str(e)
        except Exception:
            logger.exception("SAR export: could not read an item")
            doc.error = "could not be read"
        if doc.error is None:
            names |= await detect_names(ner, [doc.title, doc.text, doc.item.reason])
        else:
            status.failed_items.append(
                SarFailedItem(
                    doc_type=doc.item.doc_type, doc_id=doc.item.doc_id, error=doc.error
                )
            )
        status.processed += 1
        status.failed = len(status.failed_items)
        await _write_status(nc, status)
    names |= await detect_names(ner, queries)

    # Pass 2: redact and render.
    redactor = Redactor(names, keep=keep)
    archive = io.BytesIO()
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, doc in enumerate(docs, 1):
            pages = _pages(doc.item)
            if doc.error is not None:
                rows.append({"n": n, "error": doc.error, "pages": pages, "counts": {}})
                continue
            seen: set[tuple[str, str]] = set()
            title = redactor.redact(doc.title, seen) or ""
            reason = redactor.redact(doc.item.reason, seen) or ""
            text = redactor.redact(doc.text, seen) or ""
            zf.writestr(
                f"documents/{n:03d}-{_slug(title)}.pdf",
                render_document(title, reason, text),
            )
            rows.append(
                {
                    "n": n,
                    "title": title,
                    "reason": reason,
                    "pages": pages,
                    "counts": counts(seen),
                }
            )
        zf.writestr("index.pdf", render_index(name, rows))
        if queries:
            zf.writestr(
                "searches.pdf",
                render_searches([redactor.redact(q) or "" for q in queries]),
            )

    result = await nc.webdav.write_file(
        status.archive_path, archive.getvalue(), "application/zip"
    )
    if result["status_code"] in (412, 423):
        raise ExportError(f"an archive already exists at {status.archive_path}")
    status.state = "done"
    await _write_status(nc, status)
