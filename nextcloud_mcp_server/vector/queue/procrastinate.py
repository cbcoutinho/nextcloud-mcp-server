"""Procrastinate-backed ingest queue — the Postgres ``TaskProducer`` + worker
(Deck #183).

This replaces NATS JetStream and the old Postgres-queue stub. The MCP server now
owns *both* sides of ingest:

- **Producer** (API role / scanner) — :class:`ProcrastinateTaskProducer.send`
  *defers* one ``ingest:process_document`` job per changed document onto the
  cheapest tier's queue (``ingest-fast``) in the per-tenant Postgres (the same
  app DB; procrastinate manages its own tables).
- **Consumer** (worker role) — ``nextcloud-mcp-server worker [--tier T]`` runs
  :func:`procrastinate.App.run_worker`, which drains its tier's queue and invokes
  the existing :func:`process_document` pipeline. A parse too poor to index hops
  the job to the next tier's queue (see :class:`TieredEscalationStrategy`), so
  cheap CPU parsing and paid OCR run on independently-scaled fleets (Deck #323).

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
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import TYPE_CHECKING

from procrastinate import (
    App,
    BaseRetryStrategy,
    Blueprint,
    JobContext,
    PsycopgConnector,
    RetryDecision,
)
from procrastinate.connector import BaseConnector
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.jobs import Job

from ...config import get_procrastinate_conninfo, get_settings
from ..scanner import DocumentTask

if TYPE_CHECKING:
    from ...client import NextcloudClient

logger = logging.getLogger(__name__)

# One queue per extraction tier (Deck #323), aligned cheapest-first with
# document_processors.escalation.TIER_LADDER. Each queue is drained by its own
# worker Deployment + KEDA ScaledObject (``SELECT count(*) FROM
# procrastinate_jobs WHERE queue_name=<queue> AND status='todo'``), so a
# CPU-bound ``fast`` fleet, an in-cluster ``structured`` fleet, and a paid
# network-bound ``ocr`` fleet scale (and fail) independently.
INGEST_QUEUE_FAST = "ingest-fast"
INGEST_QUEUE_STRUCTURED = "ingest-structured"
INGEST_QUEUE_OCR = "ingest-ocr"

# tier -> queue. The producer always defers onto the cheapest tier's queue; a
# low-quality parse hops the job up the ladder via the retry strategy below.
TIER_QUEUES: dict[str, str] = {
    "fast": INGEST_QUEUE_FAST,
    "structured": INGEST_QUEUE_STRUCTURED,
    "ocr": INGEST_QUEUE_OCR,
}
_QUEUE_TIERS: dict[str, str] = {queue: tier for tier, queue in TIER_QUEUES.items()}
ALL_INGEST_QUEUES: tuple[str, ...] = tuple(TIER_QUEUES.values())
# New jobs start here; ``ocr`` is reached only by escalation.
DEFAULT_INGEST_QUEUE = INGEST_QUEUE_FAST

# Legacy single-queue name (pre-#323). A rolling upgrade may still have jobs
# parked on it; a worker can be told to drain it alongside the tier queues, and
# the job-count / reclaim helpers include it so nothing is stranded.
LEGACY_INGEST_QUEUE = "ingest"
# Back-compat alias for callers that imported the old single-queue constant.
INGEST_QUEUE_NAME = DEFAULT_INGEST_QUEUE

# Maintenance queue carrying ONLY the periodic stalled-job reclaim (no document
# jobs). Every worker drains it regardless of --tier, so the reclaim fires even
# in an asymmetric deployment where the fast fleet is scaled to zero and only
# ocr workers run. procrastinate's periodic-defer dedup ensures exactly one
# worker runs each tick even when many drain this queue. Kept off document
# queues so tier isolation (which fleet processes which docs) is preserved.
INGEST_QUEUE_MAINTENANCE = "ingest-maintenance"

# Queues the job-count + reclaim helpers sweep (tier queues + the legacy one).
_MANAGED_QUEUES: tuple[str, ...] = (*ALL_INGEST_QUEUES, LEGACY_INGEST_QUEUE)

# Blueprint namespace → registered task names are prefixed ``ingest:``.
_NAMESPACE = "ingest"
INGEST_TASK_NAME = f"{_NAMESPACE}:process_document"


def tier_for_queue(queue: str | None) -> str:
    """Tier a worker on ``queue`` should run. Unknown/legacy -> ``fast``.

    The queue-aware task uses this to pick which single tier to parse with: the
    job's current queue *is* its tier. A job on the legacy ``ingest`` queue (or
    any unrecognised queue) defaults to the cheapest tier.
    """
    return _QUEUE_TIERS.get(queue or "", "fast")


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
    context: JobContext,
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
    """Worker entry: rebuild the DocumentTask, resolve creds, run the pipeline.

    Queue-aware (Deck #323): the tier this worker runs is the tier of the job's
    current queue. A low-quality parse raises ``EscalateError``, which the
    :class:`TieredEscalationStrategy` turns into a queue-hop to the next tier.
    When per-tier escalation is disabled (``INGEST_ESCALATION_ENABLED=false``),
    ``tier`` stays ``None`` and the inline pipeline runs (fast -> OCR in one
    call), reproducing the pre-#323 single-queue behaviour.
    """
    # Local imports avoid a heavy import chain at blueprint-definition time
    # (this module is also imported by the API pod just to defer jobs).
    from ..oauth_sync import NotProvisionedError  # noqa: PLC0415
    from ..processor import process_document  # noqa: PLC0415

    tier = (
        tier_for_queue(context.job.queue)
        if get_settings().ingest_escalation_enabled
        else None
    )

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
        await process_document(task, nc_client, max_retries=1, tier=tier)
    finally:
        await nc_client.close()


async def reclaim_stalled_ingest_jobs(context: JobContext, timestamp: int) -> None:
    """Re-queue ingest jobs orphaned in ``doing`` by a crashed worker.

    procrastinate prunes dead *workers* but does not reset their in-flight jobs;
    without this they'd sit in ``doing`` forever. ``timestamp`` is procrastinate's
    periodic-run marker (unused).
    """
    manager = context.app.job_manager
    settings = get_settings()
    # Stagger the re-run rather than retry at now(): a stall is often systemic (a
    # Qdrant / embedding outage stalls every in-flight job), so reclaiming the
    # whole batch immediately every tick would thundering-herd a recovering
    # dependency, bypassing TieredEscalationStrategy's per-job backoff. The fixed
    # delay spreads them out; 0 restores the legacy immediate retry.
    retry_at = datetime.now(tz=timezone.utc) + timedelta(
        seconds=settings.ingest_reclaim_retry_delay_seconds
    )
    stalled_after = settings.ingest_stalled_job_seconds
    reclaimed = 0
    # queue=None sweeps every queue, so an orphaned job on any tier queue is
    # reclaimed regardless of which tier's worker happens to run this periodic.
    # retry_job_by_id_async keeps the job on its own queue, so a stalled ``ocr``
    # job re-runs on ``ingest-ocr`` (the ocr fleet), not the reclaiming worker's.
    for job in await manager.get_stalled_jobs(seconds_since_heartbeat=stalled_after):
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


def _first_leaf(exc: BaseException) -> BaseException:
    """Descend nested ExceptionGroups to the first concrete leaf exception.

    An anyio task group can wrap the real cause (and nest groups); the retry
    strategy classifies on the leaf, mirroring ``processor._drop_reason``.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _is_transient_infra_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a transient infra blip worth a SAME-tier retry.

    Mirrors the retryable subset of ``processor._drop_reason``: doc-fetch /
    embed / Qdrant timeouts, connection drops, rate limits, and 5xx. A parse
    that is merely *poor* never reaches here -- that path raises
    ``EscalateError`` (handled separately) -- so this is purely about
    infrastructure that should recover on its own. Imports are lazy: this only
    runs in the worker, and the module is also imported by the API pod to defer.
    """
    import httpx  # noqa: PLC0415

    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    try:
        import openai  # noqa: PLC0415

        if isinstance(
            exc,
            (
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.RateLimitError,
            ),
        ):
            return True
        if isinstance(exc, openai.APIStatusError):
            return exc.status_code >= 500
    except ImportError:  # pragma: no cover -- openai is a hard dependency
        pass
    # Deliberately over-broad: this treats ALL qdrant_client exceptions as
    # transient (not just timeouts/5xx). In a healthy cluster qdrant errors are
    # transient, and a bounded same-tier retry is cheap; a genuinely permanent
    # qdrant fault (e.g. schema mismatch) just exhausts the transient cap and
    # then gives up. So unlike _drop_reason (which only *labels* the cause), this
    # may add a few retries on a non-retriable qdrant error -- an acceptable
    # trade for not having to enumerate qdrant's non-retriable status codes.
    if type(exc).__module__.startswith("qdrant_client"):
        return True
    return False


class TieredEscalationStrategy(BaseRetryStrategy):
    """Native procrastinate retry that escalates across tier queues (Deck #323).

    Three outcomes, decided from the raised exception:

    - ``EscalateError`` -> ``RetryDecision(queue=<next tier's queue>)``: the SAME
      job hops to the next fleet's queue and is parsed once by that tier. This is
      how a document is "requeued on a failed parse" -- once per tier, with no
      same-tier parse retry.
    - a whitelisted transient infra error (doc fetch / embed / Qdrant blip) ->
      same-queue exponential backoff, while under ``max_transient_attempts``.
    - anything else, the transient cap is reached, or the target tier is unknown
      -> ``None`` (no retry); the placeholder was already marked failed by the
      pipeline, and the next scan re-picks the document.

    Per-tier attempt accounting is intentionally approximate: a queue-hop can't
    reset ``job.attempts`` (procrastinate has no per-tier counter), so parse
    escalations *do* advance the same counter the transient cap reads. Because a
    parse escalation hops (it never retries in place) the "once per parse per
    tier" guarantee is structural; the cap is just a generous global ceiling on
    transient churn across the whole lineage, not an exact per-tier count.
    """

    def __init__(self, *, max_transient_attempts: int) -> None:
        self._max_transient_attempts = max_transient_attempts

    def get_retry_decision(
        self, *, exception: BaseException, job: Job
    ) -> RetryDecision | None:
        # Lazy import: EscalateError lives in the document stack, which the API
        # pod (it also builds this App to defer) must not load. get_retry_decision
        # runs only in the worker, where the stack is already imported.
        from ...document_processors.escalation import EscalateError  # noqa: PLC0415

        exc = _first_leaf(exception)

        if isinstance(exc, EscalateError):
            queue = TIER_QUEUES.get(exc.to_tier)
            if queue is None:
                # Unknown target tier: don't strand the job on a queue no worker
                # drains -- stop and let the placeholder/next scan handle it.
                logger.error(
                    "ingest.escalate_unknown_tier from=%s to=%s",
                    exc.from_tier,
                    exc.to_tier,
                )
                return None
            logger.info(
                "ingest.escalate from=%s to=%s reason=%s queue=%s",
                exc.from_tier,
                exc.to_tier,
                exc.reason,
                queue,
            )
            # Immediate hop -- the next tier's fleet should pick it up at once.
            return RetryDecision(queue=queue, retry_in={"seconds": 0})

        if (
            _is_transient_infra_error(exc)
            and job.attempts < self._max_transient_attempts
        ):
            # 4, 8, 16, ... seconds, capped at 5 min. attempts is >=1 here (the
            # failing attempt is counted), so attempts-1 makes the first wait 4s.
            wait = min(4 * (2 ** max(0, job.attempts - 1)), 300)
            return RetryDecision(retry_in={"seconds": wait})

        return None


def _build_ingest_blueprint() -> Blueprint:
    """Create a fresh Blueprint with the ingest tasks registered.

    Fresh per call because ``add_tasks_from`` mutates the blueprint's task names
    (namespace prefixing), so the same Blueprint cannot be reused across Apps.
    """
    bp = Blueprint()
    # Durable retry owned by the queue (survives worker crashes); the in-process
    # retry loop in process_document is disabled on this path via max_retries=1.
    # The task's default queue is the cheapest tier; the producer defers there
    # explicitly and the strategy hops a job up the ladder on a poor parse.
    bp.task(  # type: ignore[no-matching-overload]
        name="process_document",
        queue=DEFAULT_INGEST_QUEUE,
        pass_context=True,
        # procrastinate's RetryValue type only admits RetryStrategy, but a custom
        # BaseRetryStrategy subclass is the documented extension point (and is
        # accepted at runtime by get_retry_strategy). The annotation is just too
        # narrow, hence the ignore.
        # Settings are snapshotted here at blueprint-build time (build_app, first
        # use), so a restart is needed to pick up INGEST_TRANSIENT_MAX_ATTEMPTS
        # changes -- intentional: the strategy lives for the App's lifetime.
        retry=TieredEscalationStrategy(
            max_transient_attempts=get_settings().ingest_transient_max_attempts
        ),
    )(process_document_task)
    # Reclaim runs on the dedicated maintenance queue (every worker drains it),
    # not a tier queue -- otherwise an ocr-only deployment (fast scaled to zero)
    # would never fire the periodic and orphaned ``doing`` jobs would never be
    # reclaimed. The task itself sweeps ALL queues (get_stalled_jobs(queue=None)).
    reclaim = bp.task(
        name="reclaim_stalled_jobs", queue=INGEST_QUEUE_MAINTENANCE, pass_context=True
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


async def get_ingest_job_counts_by_queue(
    app: App | None = None,
) -> dict[str, dict[str, int]]:
    """Per-queue ingest job counts by status (Deck #323).

    Returns ``{queue_name: {status: count}}`` for the managed ingest queues (the
    per-tier queues + the legacy single queue) that have rows. Reads
    procrastinate's per-queue stats via the manager API (not hand-written SQL) so
    a future schema bump doesn't silently break the status surface. Assumes the
    app's connector is already open. Feeds the per-tier status surface + the
    ``astrolabe_ingest_queue_depth`` gauge.
    """
    app = app or get_procrastinate_app()
    by_queue: dict[str, dict[str, int]] = {}
    for row in await app.job_manager.list_queues_async():
        name = row.get("name")
        if name not in _MANAGED_QUEUES:
            continue
        per = by_queue.setdefault(name, {})
        for status in _JOB_STATUSES:
            if status in row:
                per[status] = per.get(status, 0) + int(row[status])
    return by_queue


async def get_ingest_job_counts(app: App | None = None) -> dict[str, int]:
    """Aggregate ingest job counts by status across all managed queues.

    Fleet-wide totals summed over the per-tier queues + the legacy queue, so
    ``pending = todo + doing`` reflects all outstanding ingest work regardless of
    which tier a document currently sits on. Per-queue breakdown:
    :func:`get_ingest_job_counts_by_queue`.
    """
    counts: dict[str, int] = {}
    for per in (await get_ingest_job_counts_by_queue(app)).values():
        for status, value in per.items():
            counts[status] = counts.get(status, 0) + value
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
        # Always defer onto the cheapest tier's queue; the escalation strategy
        # hops the job up the ladder on a poor parse. queueing_lock is a global
        # partial-unique on status='todo', so a doc mid-escalation on a higher
        # tier still dedupes a fresh enqueue here -- no double-processing.
        deferrer = self._app.configure_task(
            INGEST_TASK_NAME, queue=DEFAULT_INGEST_QUEUE, queueing_lock=key
        )
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

    async def job_counts_by_queue(self) -> dict[str, dict[str, int]]:
        """Per-tier-queue ingest job counts by status (Deck #323)."""
        return await get_ingest_job_counts_by_queue(self._app)

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
