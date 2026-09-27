"""Subject access request cases (ADR-040).

A case is one file, ``<folder>/<case name>/sar-case.json``, in the user's
Nextcloud. Keeping it there leaves the subject's identifiers in the customer's
system of record, makes Nextcloud's permissions and sharing the access model
(whoever can write the case folder can work on the case), and gives an edit
history through file versions.

A case is addressed by its **case id**, the Nextcloud file id of
``sar-case.json``: it survives moves and renames and is the same for every user
the folder is shared with, while the path differs per user. Resolving the id is
a WebDAV SEARCH by fileid, which is also the access check.

Every change is a read-modify-write guarded by the file's ETag, serialised per
case within this process, and verified by re-reading. On a conflict or a lost
write the change is re-applied to the fresh copy, because changes are
operations ("add these items"), not whole-document replaces.

The same functions back the MCP tools and the management API.
"""

import logging
import posixpath
import random
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

import anyio
from anyio.abc import TaskGroup
from httpx import HTTPStatusError
from pydantic import ValidationError

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.client.webdav import like_predicate
from nextcloud_mcp_server.models.sar import (
    MAX_CASE_ITEMS,
    MAX_CASE_QUERIES,
    SarCase,
    SarCaseExport,
    SarCaseItemsChange,
    SarCaseListResponse,
    SarCaseResponse,
    SarCaseSummary,
    SarCaseUpdate,
    SarExportStatus,
    SarQueryIn,
    SarQueryLog,
)
from nextcloud_mcp_server.providers.ner import NerClient
from nextcloud_mcp_server.sar_export import (
    ExportError,
    archive_paths,
    normalize_folder,
    read_status,
    start_export,
    validate_name,
)

logger = logging.getLogger(__name__)

CASE_FILE = "sar-case.json"
_WRITE_ATTEMPTS = 6
# Write ids kept in the case for the lineage check; far more than the writes
# that can overlap one attempt.
_LINEAGE = 50
_LIST_CONCURRENCY = 20
_DEFAULT_PAGE = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _abs(path: str) -> str:
    return "/" + path.strip("/")


# --- Storage -----------------------------------------------------------------


async def _resolve(nc: NextcloudClient, case_id: int) -> str:
    """The case file's path for this user, or 404 (also when not accessible)."""
    where = (
        "<d:eq><d:prop><oc:fileid/></d:prop>"
        f"<d:literal>{int(case_id)}</d:literal></d:eq>"
    )
    found = await nc.webdav.search_files(
        scope="", where_conditions=where, properties=["fileid"], limit=1
    )
    if not found or posixpath.basename(found[0]["path"]) != CASE_FILE:
        raise ExportError(f"no SAR case {case_id}", 404)
    return _abs(found[0]["path"])


async def _load(nc: NextcloudClient, path: str) -> tuple[SarCase, str | None]:
    content, _, etag = await nc.webdav.read_file(path)
    return SarCase.model_validate_json(content), etag


class _Retry(Exception):
    """This attempt lost a race; re-read and re-apply the change."""


# One lock per case in this process: edits to a case from this server queue
# behind each other instead of racing through Nextcloud, where a stress test
# showed concurrent read-modify-writes losing acknowledged changes (reads during
# a write can return inconsistent content). Other processes, other replicas and
# other clients are caught by the lineage check in _mutate.
# ponytail: per-process lock; move the case to a database row if many replicas
# ever edit one case concurrently.
# ponytail: one small lock per case id this process has changed, never evicted;
# bounded by the cases a process ever touches. Drop a lock on close (or use an
# LRU) if a process ever serves very many cases.
_case_locks: dict[int, anyio.Lock] = {}


async def _attempt(
    nc: NextcloudClient, case_id: int, change: Callable[[SarCase], None]
) -> tuple[str, SarCase]:
    path = await _resolve(nc, case_id)
    try:
        case, etag = await _load(nc, path)
    except ValidationError as e:  # a read torn by a concurrent write
        raise _Retry from e
    if etag is None:
        raise _Retry
    change(case)
    write_id = uuid.uuid4().hex
    case.recent_writes = [*case.recent_writes, write_id][-_LINEAGE:]
    case.updated_at = _now()
    result = await nc.webdav.write_file(
        path, case.model_dump_json(indent=2).encode(), "application/json", if_match=etag
    )
    if result["status_code"] in (412, 423):
        raise _Retry
    # Lineage check: every later write built on ours carries our id forward, so
    # a re-read without it means our write was overwritten from a stale copy.
    try:
        stored, _ = await _load(nc, path)
    except ValidationError as e:
        raise _Retry from e
    if write_id not in stored.recent_writes:
        logger.warning(
            "SAR case %s: a concurrent write lost ours; re-applying", case_id
        )
        raise _Retry
    return path, case


async def _mutate(
    nc: NextcloudClient, case_id: int, change: Callable[[SarCase], None]
) -> tuple[str, SarCase]:
    """Apply ``change`` to the case and write it back, re-applying it on conflict.

    ``change`` mutates a freshly read case in place and raises
    :class:`ExportError` to refuse (e.g. the case is not open); it may run more
    than once, so it must derive everything from the case it is given.
    """
    lock = _case_locks.setdefault(case_id, anyio.Lock())
    async with lock:
        for attempt in range(_WRITE_ATTEMPTS):
            try:
                return await _attempt(nc, case_id, change)
            except _Retry:
                # Jittered backoff so writers from other processes spread out
                # instead of retrying in lockstep; none after the last attempt.
                if attempt + 1 < _WRITE_ATTEMPTS:
                    await anyio.sleep(random.uniform(0.05, 0.25) * (attempt + 1))
    raise ExportError("the case is being changed by someone else; try again", 409)


def _require(case: SarCase, *states: str) -> None:
    if case.state not in states:
        raise ExportError(
            f"the case is {case.state.replace('_', ' ')}; this needs it to be "
            + " or ".join(s.replace("_", " ") for s in states),
            409,
        )


async def _response(
    nc: NextcloudClient,
    case_id: int,
    path: str,
    case: SarCase,
    offset: int = 0,
    limit: int = _DEFAULT_PAGE,
) -> SarCaseResponse:
    latest = None
    if case.exports:
        try:
            latest = await read_status(nc, case.exports[-1].status_path)
        except (ExportError, HTTPStatusError) as e:
            # The case still records the export's outcome; only live progress
            # is missing (e.g. the output folder was moved).
            logger.debug("SAR case %s: no export status (%s)", case_id, e)
            latest = None
    total = len(case.items)
    page = case.model_copy(update={"items": case.items[offset : offset + limit]})
    return SarCaseResponse(
        case_id=case_id,
        path=path,
        case=page,
        items_total=total,
        items_offset=offset,
        latest_export=latest,
    )


# --- Operations ----------------------------------------------------------------


def _raise_if_unavailable(e: HTTPStatusError, path: str) -> None:
    """A 5xx is Nextcloud failing (maintenance, timeout), not a permissions
    problem: say so, as a retryable 503."""
    if e.response.status_code >= 500:
        raise ExportError(
            f"Nextcloud could not write {path!r} "
            f"(HTTP {e.response.status_code}); try again.",
            503,
        ) from e


async def create_case(
    nc: NextcloudClient,
    *,
    folder: str,
    name: str,
    subject: list[str],
    description: str = "",
) -> SarCaseResponse:
    """Create ``<folder>/<name>/sar-case.json``.

    Raises:
        ExportError: invalid name or folder (400), the folder is missing or not
            writable (403), or a case of that name exists there (409).
    """
    case_dir = f"{normalize_folder(folder)}/{validate_name(name)}"
    case_file = f"{case_dir}/{CASE_FILE}"
    try:
        created = await nc.webdav.create_directory(case_dir)
    except HTTPStatusError as e:
        _raise_if_unavailable(e, case_dir)
        raise ExportError(
            f"Cannot create {case_dir!r} (HTTP {e.response.status_code}): "
            f"{folder!r} must exist and be writable.",
            403,
        ) from e
    if created.get("status_code") == 405:  # MKCOL on an existing folder
        raise ExportError(f"{case_dir} already exists", 409)
    now = _now()
    case = SarCase(
        name=name,
        description=description,
        created_by=nc.username,
        created_at=now,
        updated_at=now,
        subject=subject,
    )
    try:
        result = await nc.webdav.write_file(
            case_file, case.model_dump_json(indent=2).encode(), "application/json"
        )
    except HTTPStatusError as e:
        _raise_if_unavailable(e, case_file)
        raise
    if result["status_code"] in (412, 423):
        raise ExportError(f"{case_file} already exists", 409)
    file_id = await nc.webdav.get_fileid(case_file)
    if file_id is None:
        raise ExportError(f"could not read back {case_file}", 502)
    return await _response(nc, int(file_id), case_file, case)


async def list_cases(nc: NextcloudClient) -> SarCaseListResponse:
    """Every case this user can see, including in shared and team folders."""
    found = await nc.webdav.search_files(
        scope="",
        where_conditions=like_predicate("d:displayname", CASE_FILE),
        properties=["fileid", "displayname"],
    )
    summaries: list[SarCaseSummary] = []
    limit = anyio.Semaphore(_LIST_CONCURRENCY)

    async def summarise(hit: dict) -> None:
        path = _abs(hit["path"])
        try:
            async with limit:
                case, _ = await _load(nc, path)
        except Exception:
            # A damaged or foreign file of the same name is not a case; skip
            # it rather than failing the whole list.
            logger.warning("Skipping unreadable SAR case file %s", path)
            return
        # Appends from concurrent tasks are safe: no await between read and write.
        summaries.append(
            SarCaseSummary(
                case_id=int(hit["file_id"]),
                path=path,
                name=case.name,
                state=case.state,
                items=len(case.items),
                updated_at=case.updated_at,
                latest_export=case.exports[-1] if case.exports else None,
            )
        )

    async with anyio.create_task_group() as tg:
        for hit in found:
            if hit.get("name") == CASE_FILE and hit.get("file_id") is not None:
                tg.start_soon(summarise, hit)
    summaries.sort(key=lambda s: s.updated_at, reverse=True)
    return SarCaseListResponse(cases=summaries)


async def get_case(
    nc: NextcloudClient, case_id: int, offset: int = 0, limit: int = _DEFAULT_PAGE
) -> SarCaseResponse:
    path = await _resolve(nc, case_id)
    case, _ = await _load(nc, path)
    return await _response(nc, case_id, path, case, offset, limit)


async def update_case(
    nc: NextcloudClient, case_id: int, update: SarCaseUpdate
) -> SarCaseResponse:
    """Edit subject/description (open cases), close, or reopen.

    Closing is final. Reopening is for a case that is ready for audit and needs
    changing and exporting again.
    """

    def change(case: SarCase) -> None:
        if update.subject is not None or update.description is not None:
            _require(case, "open")
            if update.subject is not None:
                case.subject = update.subject
            if update.description is not None:
                case.description = update.description
        if update.state == "closed":
            _require(case, "open", "ready_for_audit")
            case.state = "closed"
            case.closed_by = nc.username
            case.closed_at = _now()
        elif update.state == "open" and case.state != "open":
            _require(case, "ready_for_audit")
            case.state = "open"

    path, case = await _mutate(nc, case_id, change)
    return await _response(nc, case_id, path, case)


async def change_items(
    nc: NextcloudClient, case_id: int, request: SarCaseItemsChange
) -> SarCaseResponse:
    """Add (or update) and remove items, and log queries, on an open case."""
    user, now = nc.username, _now()

    def change(case: SarCase) -> None:
        _require(case, "open")
        _merge_items(case, request, user, now)
        _log_queries(case, request.queries, user, now)

    path, case = await _mutate(nc, case_id, change)
    return await _response(nc, case_id, path, case)


def _merge_items(
    case: SarCase, request: SarCaseItemsChange, user: str, now: str
) -> None:
    """Remove, then add or update, keeping who first added an item."""
    removed = {(r.doc_type, r.doc_id) for r in request.remove}
    items = {
        (i.doc_type, i.doc_id): i
        for i in case.items
        if (i.doc_type, i.doc_id) not in removed
    }
    for item in request.add:
        key = (item.doc_type, item.doc_id)
        existing = items.get(key)
        if existing is None:
            items[key] = item.model_copy(update={"added_by": user, "added_at": now})
            continue
        items[key] = item.model_copy(
            update={
                "added_by": existing.added_by,
                "added_at": existing.added_at,
                # Keep what an earlier add knew if this one omits it.
                "title": item.title or existing.title,
                "found_by": item.found_by or existing.found_by,
            }
        )
    if len(items) > MAX_CASE_ITEMS:
        raise ExportError(f"a case holds at most {MAX_CASE_ITEMS} items", 400)
    case.items = list(items.values())


def _query_key(q: SarQueryIn | SarQueryLog) -> tuple[str, str]:
    return q.text, q.filters.model_dump_json() if q.filters else ""


def _log_queries(case: SarCase, queries: list[SarQueryIn], user: str, now: str) -> None:
    """Log searches, except ones already logged (same text, same filters):
    re-running a search adds nothing to the record, only to the file."""
    logged = {_query_key(q) for q in case.queries}
    for q in queries:
        if _query_key(q) in logged:
            continue
        logged.add(_query_key(q))
        case.queries.append(
            SarQueryLog(
                text=q.text, hits=q.hits, filters=q.filters, run_by=user, run_at=now
            )
        )
    if len(case.queries) > MAX_CASE_QUERIES:
        raise ExportError(f"a case logs at most {MAX_CASE_QUERIES} searches", 400)


def _require_exportable(case: SarCase) -> None:
    if not case.items:
        raise ExportError("add at least one document before exporting", 400)
    if any(not i.reason.strip() for i in case.items):
        raise ExportError("give a reason for every document before exporting", 400)


def _mark_export(
    case: SarCase,
    version: int,
    state: Literal["done", "failed"],
    message: str | None,
    failed: int | None = None,
) -> None:
    """Record how export ``version`` ended."""
    for export in case.exports:
        if export.version == version:
            export.state = state
            export.message = message
            if failed is not None:
                export.failed = failed


async def export_case(
    nc: NextcloudClient,
    background: NextcloudClient,
    ner: NerClient,
    task_group: TaskGroup | None,
    case_id: int,
    output_folder: str | None = None,
) -> SarCaseResponse:
    """Lock the case and start a new export version in the background.

    Takes ownership of ``background``, a client that outlives the request:
    the export job closes it when done, or this function does if the job never
    starts. When the job ends it marks the case ready for audit, or open again
    if it failed.
    """
    started = False
    try:
        started = await _start_case_export(
            nc, background, ner, task_group, case_id, output_folder
        )
    finally:
        if not started:
            await background.close()
    return await get_case(nc, case_id)


async def _start_case_export(
    nc: NextcloudClient,
    background: NextcloudClient,
    ner: NerClient,
    task_group: TaskGroup | None,
    case_id: int,
    output_folder: str | None,
) -> bool:
    path = await _resolve(nc, case_id)
    case, _ = await _load(nc, path)
    _require(case, "open")
    _require_exportable(case)
    version = len(case.exports) + 1
    folder = output_folder or posixpath.join(posixpath.dirname(path), "exports")
    name = f"{case.name}-v{version}"
    archive_path, status_path = archive_paths(folder, name)  # validates
    if output_folder is None:
        # The case's own exports/ folder; already there after the first export.
        await nc.webdav.create_directory(folder)
    submitted = SarCaseExport(
        version=version,
        state="running",
        archive_path=archive_path,
        status_path=status_path,
        submitted_by=nc.username,
        submitted_at=_now(),
        total=len(case.items),
    )

    # What the export works from is taken from the copy the lock is written
    # to, not from the read above: an item added in between would otherwise be
    # in the case but missing from its archive.
    locked: list[SarCase] = []

    def lock(c: SarCase) -> None:
        _require(c, "open")
        if len(c.exports) + 1 != version:
            raise ExportError("another export was just started; reload", 409)
        _require_exportable(c)
        submitted.total = len(c.items)
        c.state = "exporting"
        c.exports.append(submitted)
        # _mutate may call this again on a retry; the last call is the write.
        locked[:] = [c.model_copy(deep=True)]

    await _mutate(nc, case_id, lock)
    snapshot = locked[0]

    async def finish(status: SarExportStatus) -> None:
        def record(c: SarCase) -> None:
            done = status.state == "done"
            _mark_export(
                c, version, "done" if done else "failed", status.message, status.failed
            )
            c.state = "ready_for_audit" if done else "open"

        await _mutate(background, case_id, record)

    try:
        await start_export(
            nc=background,
            ner=ner,
            task_group=task_group,
            output_folder=folder,
            name=name,
            keep=list(snapshot.subject),
            items=list(snapshot.items),
            queries=[q.describe() for q in snapshot.queries],
            on_finish=finish,
        )
    except BaseException as e:
        message = str(e) if isinstance(e, ExportError) else "could not start"

        def unlock(c: SarCase) -> None:
            c.state = "open"
            _mark_export(c, version, "failed", message)

        # Shielded: on cancellation the unlock would otherwise be cancelled
        # too, leaving the case locked in "exporting" for good.
        with anyio.CancelScope(shield=True):
            await _mutate(nc, case_id, unlock)
        raise
    return True
