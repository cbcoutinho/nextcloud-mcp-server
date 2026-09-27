"""Redacted export archives for subject access requests (ADR-040).

An export takes the documents an operator selected, redacts every third party
(names, addresses, emails, phone numbers, NI numbers) while keeping the
subject's own identifiers, renders each document to PDF and writes one zip to
an output folder in Nextcloud:

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pymupdf
from anyio import CapacityLimiter, to_thread
from anyio.abc import TaskGroup
from httpx import HTTPStatusError
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.models.sar import (
    SarExportStatus,
    SarFailedItem,
    SarItem,
)
from nextcloud_mcp_server.providers.ner import NerClient
from nextcloud_mcp_server.redaction import (
    Redactor,
    counts,
    detect_entities,
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


def validate_name(name: str) -> str:
    """An archive or case name: one plain path segment.

    Raises:
        ExportError: not a plain name (400).
    """
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise ExportError(
            "name must be 1-100 letters, digits, spaces, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return name


def normalize_folder(folder: str) -> str:
    """``folder`` as an absolute path ("" for the root), refusing ``.``/``..``.

    User-controlled. The user's own credentials bound what can be written,
    but a ``..`` segment is refused outright rather than left to the server's
    path normalisation.
    """
    segments = [s for s in folder.strip().split("/") if s]
    if any(s in (".", "..") for s in segments):
        raise ExportError("folder must not contain '.' or '..' segments")
    return "/" + "/".join(segments) if segments else ""


def archive_paths(output_folder: str, name: str) -> tuple[str, str]:
    """``(archive_path, status_path)`` for an export, validating both inputs."""
    folder = normalize_folder(output_folder)
    validate_name(name)
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
    owners = await _index_owners(nc, user_id, item)
    chunks = _in_requested_pages(await _indexed_chunks(user_id, owners, item), item)
    title = str(chunks[0].get("title") or f"{item.doc_type} {item.doc_id}")
    text = stitch(
        [
            (int(p.get("chunk_start_offset") or 0), str(p.get("excerpt") or ""))
            for p in chunks
        ]
    )
    return title, text


async def _index_owners(
    nc: NextcloudClient, user_id: str, item: SarItem
) -> list[str] | None:
    """Whose index entries may hold the item: shared owners for a file the user
    can open right now, else the user only (``None``)."""
    if item.doc_type != "file":
        # ponytail: non-file items rely on the index's ownership filter
        # (self-only) without a live existence check; add one per doc type via
        # search/verification.py if deleted notes/cards ever reach an export.
        return None
    # Live check: the file still exists and this user can open it. Only then
    # widen the index lookup to owners who share with the user, as
    # search/context.py does for context expansion.
    if not item.doc_id.isdigit() or not await nc.webdav.file_accessible_by_id(
        int(item.doc_id)
    ):
        raise _ItemError("not accessible to the requesting user")
    return await list_accessible_owners(nc.sharing, user_id)


async def _indexed_chunks(
    user_id: str, owners: list[str] | None, item: SarItem
) -> list[dict[str, Any]]:
    """The item's indexed chunks, one per chunk index."""
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
    return list(payloads.values())


def _in_requested_pages(
    chunks: list[dict[str, Any]], item: SarItem
) -> list[dict[str, Any]]:
    """The chunks within the item's page range; all of them without one."""
    if item.page_start is None and item.page_end is None:
        return chunks
    if all(p.get("page_number") is None for p in chunks):
        raise _ItemError("a page range was given but the document has no pages")
    first = item.page_start or 1
    last = item.page_end if item.page_end is not None else 10**9
    selected = [p for p in chunks if _in_pages(p, first, last)]
    if not selected:
        raise _ItemError("no indexed text in the requested pages")
    return selected


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
        "<p>Third-party names, postal addresses, email addresses, phone numbers "
        "and NI numbers are "
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


async def read_status(nc: NextcloudClient, status_path: str) -> SarExportStatus:
    """The recorded status of an export.

    Raises:
        ExportError: no such export (404).
    """
    try:
        content, _, _ = await nc.webdav.read_file(status_path)
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ExportError(f"no export status at {status_path}", 404) from e
        raise
    return SarExportStatus.model_validate_json(content)


async def background_client(user_id: str) -> NextcloudClient:
    """A client for ``user_id`` that outlives the request, for the export job.

    Raises:
        ExportError: the user has not provisioned background access (403).
    """
    try:
        return await resolve_background_client(user_id)
    except NotProvisionedError as e:
        raise ExportError(
            "SAR export runs in the background and needs background access: "
            "provision it in Astrolabe's personal settings.",
            status=403,
        ) from e


OnFinish = Callable[[SarExportStatus], Awaitable[None]]


async def start_export(
    *,
    nc: NextcloudClient,
    ner: NerClient,
    task_group: TaskGroup | None,
    output_folder: str,
    name: str,
    keep: list[str],
    items: list[SarItem],
    queries: list[str],
    on_finish: OnFinish | None = None,
) -> SarExportStatus:
    """Check the output folder, write the initial status, start the job.

    ``nc`` must be a client that outlives the request (see
    :func:`background_client`). On success the job owns it and closes it;
    on failure the caller still owns it. ``on_finish`` runs when the job ends,
    successful or not, before the client is closed.

    Raises:
        ExportError: no task group (503), invalid name or folder (400), the
            folder is missing or not writable (403), or the name is taken (409).
    """
    if task_group is None:
        raise ExportError(
            "SAR export is unavailable: background tasks not running", 503
        )
    archive_path, status_path = archive_paths(output_folder, name)
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
    try:
        await _write_status(nc, status, create=True)
    except HTTPStatusError as e:
        raise ExportError(
            f"Cannot write to {output_folder!r} "
            f"(HTTP {e.response.status_code}): it must exist and be writable.",
            status=403,
        ) from e
    task_group.start_soon(
        _run_and_close, nc, ner, status, name, keep, items, queries, on_finish
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
    on_finish: OnFinish | None = None,
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
    try:
        if on_finish is not None:
            await on_finish(status)
    except Exception:
        logger.exception("SAR export finish hook failed for %s", status.archive_path)
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

    # Pass 1: read every document and detect names and addresses across all of
    # them, so one set (and one numbering) covers the archive.
    # ponytail: texts are held in memory for the whole export; fine for
    # hundreds of documents, re-read per pass if archives grow far beyond that.
    names: set[str] = set()
    addresses: set[str] = set()
    for doc in docs:
        try:
            doc.title, doc.text = await document_text(nc, user_id, doc.item)
        except _ItemError as e:
            doc.error = str(e)
        except Exception:
            logger.exception("SAR export: could not read an item")
            doc.error = "could not be read"
        if doc.error is None:
            found, places = await detect_entities(
                ner, [doc.title, doc.text, doc.item.reason]
            )
            names |= found
            addresses |= places
        else:
            status.failed_items.append(
                SarFailedItem(
                    doc_type=doc.item.doc_type, doc_id=doc.item.doc_id, error=doc.error
                )
            )
        status.processed += 1
        status.failed = len(status.failed_items)
        await _write_status(nc, status)
    found, places = await detect_entities(ner, queries)
    names |= found
    addresses |= places

    # Pass 2: redact and render, on a worker thread: regexes, PDF layout and
    # deflate for every document would otherwise hold the event loop.
    redactor = Redactor(names, keep=keep, addresses=addresses)
    archive = await to_thread.run_sync(
        _build_archive, name, docs, queries, redactor, limiter=_render_limiter()
    )

    result = await nc.webdav.write_file(status.archive_path, archive, "application/zip")
    if result["status_code"] in (412, 423):
        raise ExportError(f"an archive already exists at {status.archive_path}")
    status.state = "done"
    await _write_status(nc, status)


# PyMuPDF is not safe to call from several threads at once, so exports take
# turns rendering.
# ponytail: one render at a time per process; render in a subprocess if
# concurrent exports ever queue behind each other.
_limiter: CapacityLimiter | None = None


def _render_limiter() -> CapacityLimiter:
    # Created on first use: a limiter needs a running event loop.
    global _limiter
    if _limiter is None:
        _limiter = CapacityLimiter(1)
    return _limiter


def _build_archive(
    name: str, docs: list["_Doc"], queries: list[str], redactor: Redactor
) -> bytes:
    """The zip: one redacted PDF per document, the index and the search log."""
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
    return archive.getvalue()
