"""Procrastinate-backed ingest queue — the Postgres ``TaskProducer`` + worker
(Deck #183).

This replaces NATS JetStream and the old Postgres-queue stub. The MCP server now
owns *both* sides of ingest:

- **Producer** (API role / scanner) — :class:`ProcrastinateTaskProducer.send`
  *defers* one ``ingest:process_document`` job per changed document into the
  per-tenant Postgres (the same app DB; procrastinate manages its own tables).
- **Consumer** (worker role) — ``nextcloud-mcp-server worker`` runs
  :func:`procrastinate.App.run_worker`, which drains the ``ingest`` queue and
  invokes the existing :func:`process_document` pipeline.

Design notes:

- **No execution ``lock``, only ``queueing_lock``.** procrastinate does NOT
  auto-reclaim ``doing`` jobs, so a per-doc execution lock would permanently
  deadlock a document if a worker crashed mid-job. The Qdrant upsert is
  idempotent (deterministic ``uuid5`` point IDs), so a concurrent/re-run is
  harmless; ``queueing_lock`` (partial-unique on ``status='todo'``) is enough to
  dedupe enqueues, and :func:`reclaim_stalled_ingest_jobs` retries jobs orphaned
  in ``doing`` by a crash.
- procrastinate is Postgres-only and uses asyncio; ``anyio`` runs natively on the
  asyncio backend, so the worker can call the anyio-based pipeline directly.
- Tasks are defined on a :class:`procrastinate.Blueprint` so the connector is
  decoupled from the task registry: production binds a real
  :class:`PsycopgConnector`; unit tests bind ``testing.InMemoryConnector``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from types import TracebackType
from typing import TYPE_CHECKING

from procrastinate import App, Blueprint, JobContext, PsycopgConnector, RetryStrategy
from procrastinate.connector import BaseConnector
from procrastinate.exceptions import AlreadyEnqueued

from ...config import get_procrastinate_conninfo, get_settings
from ..scanner import DocumentTask

if TYPE_CHECKING:
    from ...client import NextcloudClient

logger = logging.getLogger(__name__)

# Single queue for document ingest. KEDA scales the worker Deployment on the
# depth of this queue (``SELECT count(*) FROM procrastinate_jobs WHERE
# queue_name='ingest' AND status='todo'``).
INGEST_QUEUE_NAME = "ingest"
# Blueprint namespace → registered task names are prefixed ``ingest:``.
_NAMESPACE = "ingest"
INGEST_TASK_NAME = f"{_NAMESPACE}:process_document"

# A crashed worker leaves its job in ``doing``; reclaim it once its (per-worker)
# heartbeat is this many seconds stale. The default is sized well above the
# longest expected ``process_document`` (PDF render + embedding) so a slow-but-
# live worker — whose heartbeat stays current during a long job — is never
# reclaimed out from under itself. Operators on slow embedding backends can tune
# it via INGEST_STALLED_JOB_SECONDS (read per-run in reclaim_stalled_ingest_jobs).


# Tasks are defined as plain functions and registered onto a *fresh* Blueprint
# per app (see _build_ingest_blueprint). procrastinate's add_tasks_from mutates
# the blueprint's task names in place (namespace prefixing), so a single shared
# Blueprint cannot be added to more than one App — which the tests (in-memory +
# real Postgres) and any re-init path require.
async def process_document_task(
    *,
    user_id: str,
    doc_id: str,
    doc_type: str,
    operation: str,
    modified_at: int,
    file_path: str | None = None,
    metadata: dict[str, int | str] | None = None,
    etag: str | None = None,
    owner_id: str | None = None,
) -> None:
    """Worker entry: rebuild the DocumentTask, resolve creds, run the pipeline."""
    # Local imports avoid a heavy import chain at blueprint-definition time
    # (this module is also imported by the API pod just to defer jobs).
    from ..oauth_sync import NotProvisionedError  # noqa: PLC0415
    from ..processor import process_document  # noqa: PLC0415

    task = DocumentTask(
        user_id=user_id,
        doc_id=doc_id,
        doc_type=doc_type,
        operation=operation,
        modified_at=modified_at,
        file_path=file_path,
        metadata=metadata,
        etag=etag,
        owner_id=owner_id,
    )
    try:
        nc_client = await _resolve_client(user_id)
    except NotProvisionedError:
        # A deprovisioned user must not pin a worker slot retrying forever.
        # Finish the job as a no-op; the next scan re-enqueues once the user
        # re-provisions an app password. Other errors (transient DB/network)
        # propagate so procrastinate's retry strategy handles them.
        logger.warning(
            "ingest.skip_no_credentials user=%s doc=%s:%s", user_id, doc_type, doc_id
        )
        return

    try:
        # Durable retry is procrastinate's job; disable the in-process loop.
        await process_document(task, nc_client, max_retries=1)
    finally:
        await nc_client.close()


async def reclaim_stalled_ingest_jobs(context: JobContext, timestamp: int) -> None:
    """Re-queue ingest jobs orphaned in ``doing`` by a crashed worker.

    procrastinate prunes dead *workers* but does not reset their in-flight jobs;
    without this they'd sit in ``doing`` forever. ``timestamp`` is procrastinate's
    periodic-run marker (unused).
    """
    manager = context.app.job_manager
    retry_at = datetime.now(tz=timezone.utc)
    stalled_after = get_settings().ingest_stalled_job_seconds
    reclaimed = 0
    for job in await manager.get_stalled_jobs(
        queue=INGEST_QUEUE_NAME, seconds_since_heartbeat=stalled_after
    ):
        if job.id is None:
            continue
        await manager.retry_job_by_id_async(job_id=job.id, retry_at=retry_at)
        reclaimed += 1
    if reclaimed:
        logger.warning("ingest.reclaimed_stalled_jobs count=%d", reclaimed)
    else:
        # Visible heartbeat under verbose logging when debugging a suspected
        # reclaim failure, without noising up production logs.
        logger.debug("ingest.reclaim_check stalled=0")


async def _resolve_client(user_id: str) -> NextcloudClient:
    """Build an authenticated NextcloudClient for ``user_id`` in the worker.

    Single-user BasicAuth uses the shared env credentials; every multi-user mode
    resolves the user's locally-stored app password (BasicAuth).
    """
    from ...client import NextcloudClient  # noqa: PLC0415
    from ...config_validators import AuthMode, detect_auth_mode  # noqa: PLC0415

    settings = get_settings()
    if detect_auth_mode(settings) == AuthMode.SINGLE_USER_BASIC:
        return NextcloudClient.from_env()

    from ..oauth_sync import get_user_client_basic_auth  # noqa: PLC0415

    host = settings.nextcloud_host
    if not host:
        raise ValueError("NEXTCLOUD_HOST is required for multi-user ingest")
    return await get_user_client_basic_auth(user_id, host)


def _build_ingest_blueprint() -> Blueprint:
    """Create a fresh Blueprint with the ingest tasks registered.

    Fresh per call because ``add_tasks_from`` mutates the blueprint's task names
    (namespace prefixing), so the same Blueprint cannot be reused across Apps.
    """
    bp = Blueprint()
    # Durable retry owned by the queue (survives worker crashes); the in-process
    # retry loop in process_document is disabled on this path via max_retries=1.
    bp.task(
        name="process_document",
        queue=INGEST_QUEUE_NAME,
        retry=RetryStrategy(max_attempts=5, exponential_wait=4),
    )(process_document_task)
    reclaim = bp.task(
        name="reclaim_stalled_jobs", queue=INGEST_QUEUE_NAME, pass_context=True
    )(reclaim_stalled_ingest_jobs)
    bp.periodic(cron="*/5 * * * *", periodic_id="reclaim_stalled_ingest")(reclaim)
    return bp


def build_app(connector: BaseConnector) -> App:
    """Build an App for the given connector with the ingest tasks registered.

    Shared by production (:func:`get_procrastinate_app`) and tests (which pass a
    ``testing.InMemoryConnector``).
    """
    app = App(connector=connector)
    app.add_tasks_from(_build_ingest_blueprint(), namespace=_NAMESPACE)
    return app


def build_app_for_url(database_url: str) -> App:
    """Build an App bound to an explicit Postgres URL (for the CLI, which may
    target a ``--database-url`` that differs from the ``DATABASE_URL`` env)."""
    return build_app(
        PsycopgConnector(conninfo=get_procrastinate_conninfo(database_url))
    )


_app: App | None = None


def get_procrastinate_app() -> App:
    """Process-wide procrastinate App bound to the Postgres app database."""
    global _app
    if _app is None:
        _app = build_app(PsycopgConnector(conninfo=get_procrastinate_conninfo()))
    return _app


async def _ingest_schema_present(app: App) -> bool:
    row = await app.connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') IS NOT NULL AS present"
    )
    return bool(row["present"])


async def _apply_ingest_queue_schema_open(app: App) -> None:
    """Apply the ingest-queue schema on an already-open connector (apply-if-absent).

    procrastinate's ``schema.sql`` uses bare ``CREATE TYPE``/``CREATE TABLE``
    (not ``IF NOT EXISTS``), so it errors if re-applied — it is meant to run
    once on a fresh DB. We skip when ``procrastinate_jobs`` already exists;
    *version* upgrades use procrastinate's own migration files (operator-run, a
    lineage independent of the app's Alembic schema).

    Safe to call concurrently across rolling-update pods without an advisory
    lock: Postgres DDL is transactional and procrastinate applies the whole
    schema in one transaction, so a pod that loses the race rolls back cleanly
    and we treat the resulting error as benign once the schema is present.
    """
    if await _ingest_schema_present(app):
        logger.debug("ingest queue schema already present; skipping apply")
        return
    try:
        await app.schema_manager.apply_schema_async()
        logger.info("Applied procrastinate ingest queue schema")
    except Exception:
        # The apply runs in a single transaction, so any failure rolls back
        # atomically (no partial schema). The only benign case is losing the
        # create race to another pod — confirmed by re-checking presence. Any
        # other failure (network, auth, …) leaves the schema absent, so this
        # branch re-raises it rather than masking it.
        if await _ingest_schema_present(app):
            logger.info("Ingest queue schema applied concurrently by another pod")
            return
        raise


async def apply_ingest_queue_schema(
    app: App | None = None, *, manage_connection: bool = True
) -> None:
    """Create procrastinate's tables on a fresh database (apply-if-absent).

    By default opens a short-lived connection, so it is safe to call standalone
    from the CLI ``db upgrade`` path. Pass ``manage_connection=False`` when the
    caller already holds an open connector (the ``worker`` command opens the App
    once and reuses it) to avoid a redundant open/close cycle.
    """
    app = app or get_procrastinate_app()
    if not manage_connection:
        await _apply_ingest_queue_schema_open(app)
        return
    async with app.open_async():
        await _apply_ingest_queue_schema_open(app)


# Job-status keys procrastinate flattens into each list_queues row (alongside
# ``name`` and ``jobs_count``). ``aborting`` is legacy/unused since v3.0.0.
_JOB_STATUSES = ("todo", "doing", "succeeded", "failed", "cancelled", "aborted")


async def get_ingest_job_counts(app: App | None = None) -> dict[str, int]:
    """Return ingest job counts by status (``todo``/``doing``/``failed``/…).

    Reads procrastinate's per-queue stats via the manager API (not hand-written
    SQL) so a future schema bump doesn't silently break the status surface. The
    manager flattens its per-status ``stats`` into top-level row keys, so we read
    the known status keys directly. Assumes the app's connector is already open.
    """
    app = app or get_procrastinate_app()
    counts: dict[str, int] = {}
    for row in await app.job_manager.list_queues_async(queue=INGEST_QUEUE_NAME):
        for status in _JOB_STATUSES:
            if status in row:
                counts[status] = counts.get(status, 0) + int(row[status])
    return counts


def _doc_queueing_lock(task: DocumentTask) -> str:
    """Per-document enqueue-dedup key (partial-unique on ``status='todo'``).

    Collision-safe with a raw ``:`` delimiter because the first two segments can
    never themselves contain ``:``:

    - ``user_id`` — a Nextcloud username/UID; Nextcloud rejects ``:`` in
      usernames (Web UI + provisioning validation), so it is colon-free.
    - ``doc_type`` — a controlled enum (``note``/``file``/``deck_card``/
      ``news_item``); a future ``doc_type`` containing ``:`` would break this
      invariant, so keep doc_type colon-free.

    The trailing ``doc_id`` may contain anything — it's the final unambiguous
    segment.
    """
    return f"{task.user_id}:{task.doc_type}:{task.doc_id}"


class ProcrastinateTaskProducer:
    """``TaskProducer`` that defers ingest jobs into Postgres via procrastinate.

    The App's connector pool is owned by the server lifespan (opened once,
    closed on shutdown), so ``clone``/``aenter``/``aexit``/``aclose`` are no-ops
    — there is no per-handle resource like the memory stream's clones.
    """

    def __init__(self, app: App):
        self._app = app

    @classmethod
    async def connect(cls) -> ProcrastinateTaskProducer:
        app = get_procrastinate_app()
        # ``App.open_async()`` returns procrastinate's dual-mode AwaitableContext:
        # ``await``-ing it opens the connector pool and leaves it open (vs the
        # ``async with`` form, which closes on block exit). The producer's pool is
        # long-lived — owned by the server lifespan and torn down once in
        # ``drain()`` (close_async) on shutdown — so the bare ``await`` is correct
        # here, unlike the scoped ``async with`` used for one-shot schema apply.
        await app.open_async()
        return cls(app)

    async def send(self, task: DocumentTask, /) -> None:
        key = _doc_queueing_lock(task)
        deferrer = self._app.configure_task(INGEST_TASK_NAME, queueing_lock=key)
        try:
            await deferrer.defer_async(**asdict(task))
        except AlreadyEnqueued:
            # A todo job already exists for this doc; the next periodic scan
            # re-evaluates freshness (placeholder/Qdrant modified_at only
            # advances after a successful index), so this is not a lost update.
            logger.debug("ingest.already_enqueued key=%s", key)

    async def ensure_schema(self) -> None:
        """Apply the ingest-queue schema on the producer's already-open pool.

        Lets the API lifespan provision the schema without a second open/close
        cycle (it already opened the connector to build this producer) — the
        ``worker`` command shares the same single-open pattern.
        """
        await _apply_ingest_queue_schema_open(self._app)

    async def job_counts(self) -> dict[str, int]:
        """Ingest job counts by status (for the vector-sync status surface)."""
        return await get_ingest_job_counts(self._app)

    def clone(self) -> ProcrastinateTaskProducer:
        return self

    async def __aenter__(self) -> ProcrastinateTaskProducer:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    # The bare suppression marker silences S7503 (async method without await):
    # ``async def`` is required by the TaskProducer protocol; per-handle close is
    # a no-op (the pool is owned by the lifespan, drained once on shutdown).
    async def aclose(self) -> None:  # NOSONAR
        return None

    async def drain(self) -> None:
        """Close the shared connector pool (lifespan shutdown only)."""
        await self._app.close_async()
