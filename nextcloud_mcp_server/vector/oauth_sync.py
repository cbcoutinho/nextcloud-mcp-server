"""Multi-user vector sync orchestration.

Manages background vector sync for multi-user deployments:
- User Manager: Monitors storage for user changes
- Per-User Scanners: One scanner task per provisioned user
- Shared Processor Pool: Processes documents from all users

Background sync authenticates as each provisioned user via locally-stored
Nextcloud app passwords (BasicAuth), retrieved through the management API
after the user completes Login Flow v2 (or, in multi-user BasicAuth mode,
the per-user Astrolabe provisioning flow).

The earlier OAuth refresh-token path was removed in the ADR-022 follow-up:
it depended on unmerged Nextcloud `user_oidc` patches for Bearer-token
validation on non-OCS endpoints, and was never reachable from any
supported deployment mode. The `TokenBrokerService` constructed in
`app.py` is retained for the management API revoke endpoint, not for
background sync.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio
from anyio.abc import TaskGroup, TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream
from httpx import BasicAuth, HTTPStatusError

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector._errors import format_exception_group
from nextcloud_mcp_server.vector.processor import process_document
from nextcloud_mcp_server.vector.queue.ports import TaskProducer
from nextcloud_mcp_server.vector.scanner import DocumentTask, scan_user_documents

logger = logging.getLogger(__name__)


class NotProvisionedError(Exception):
    """User has not provisioned offline access or has revoked it."""

    pass


class ProvisionSignal:
    """One-shot doorbell that wakes ``user_manager_task`` on demand.

    A provisioning request rings this (``ring()``) right after storing a new
    user's app password so the manager re-polls immediately instead of waiting
    out ``VECTOR_SYNC_USER_POLL_INTERVAL``. The manager parks on ``wait()``;
    each ring releases exactly one wait, after which the underlying event is
    re-armed for the next cycle.

    The reference is stable for the life of the lifespan (stored once on the
    ``VectorSyncState`` singleton), so the manager never has to republish a new
    event back to shared state — avoiding any ``app`` ↔ ``vector`` import cycle.

    Concurrency: ``anyio.Event`` is sticky, so a ``ring()`` that lands before
    ``wait()`` is still observed. ``wait()`` re-arms in a ``finally`` with no
    ``await`` before the swap, so under cooperative scheduling a concurrent
    ``ring()`` cannot slip into that window and be lost — and the re-arm also
    runs if ``wait()`` is cancelled (e.g. shutdown racing the doorbell), leaving
    a fresh unset event rather than a stale set-but-consumed one.
    """

    def __init__(self) -> None:
        self._event = anyio.Event()

    def ring(self) -> None:
        """Signal a pending wait (or the next one to arrive)."""
        self._event.set()

    async def wait(self) -> None:
        """Block until the next ring, then re-arm for the following cycle."""
        try:
            await self._event.wait()
        finally:
            # Re-arm even on cancellation. The assignment is not a checkpoint,
            # so no concurrent ring() can interleave before the swap; a ring
            # that already arrived lands on the fresh event for the next wait().
            self._event = anyio.Event()


# Process-wide app-password storage for the BasicAuth client path.
#
# get_user_client_basic_auth is on the search hot path (Unified Search and the
# /api/v1 viz endpoints call it per request). Creating a fresh
# RefreshTokenStorage and running ``initialize()`` — a full Alembic upgrade in
# a worker thread — on every call is both wasteful and unsafe: concurrent
# upgrades race on Alembic's non-thread-safe module-global EnvironmentContext
# proxy, surfacing as ``KeyError: 'script'``. Cache one initialized instance,
# guarded by a lock so the one-time migration runs exactly once. The lock is
# created lazily inside an async context (anyio primitives must not be built at
# import time — trio compatibility), mirroring vector/qdrant_client.py.
_basic_auth_storage: "RefreshTokenStorage | None" = None
_basic_auth_storage_lock: anyio.Lock | None = None


async def _get_initialized_basic_auth_storage() -> "RefreshTokenStorage":
    """Return the process-wide, already-initialized app-password storage."""
    global _basic_auth_storage, _basic_auth_storage_lock
    if _basic_auth_storage is not None:
        return _basic_auth_storage
    # Safe under cooperative scheduling: no await between the None-check and the
    # assignment, so two coroutines cannot both create a lock.
    if _basic_auth_storage_lock is None:
        _basic_auth_storage_lock = anyio.Lock()
    async with _basic_auth_storage_lock:
        if _basic_auth_storage is None:
            storage = RefreshTokenStorage.from_env()
            await storage.initialize()
            _basic_auth_storage = storage
    return _basic_auth_storage


@dataclass
class UserSyncState:
    """State for a single user's scanner task."""

    user_id: str
    cancel_scope: anyio.CancelScope
    started_at: float = field(default_factory=time.time)


async def get_user_client_basic_auth(
    user_id: str,
    nextcloud_host: str,
    storage: "RefreshTokenStorage | None" = None,
) -> NextcloudClient:
    """Get an authenticated NextcloudClient using app password (BasicAuth mode).

    For multi-user BasicAuth deployments where users provision app passwords
    via Astrolabe personal settings. The app password is stored locally in the
    MCP server's database after being provisioned through the management API.

    Args:
        user_id: User identifier
        nextcloud_host: Nextcloud base URL
        storage: Optional RefreshTokenStorage instance (created from env if not provided)

    Returns:
        Authenticated NextcloudClient with BasicAuth

    Raises:
        NotProvisionedError: If user has not provisioned an app password
    """
    # Get or create storage instance. Reuse a process-wide initialized instance
    # rather than building one (and running an Alembic upgrade) per call — see
    # _get_initialized_basic_auth_storage for why (hot path + Alembic race).
    if storage is None:
        storage = await _get_initialized_basic_auth_storage()

    # Retrieve app password (and the stored Nextcloud loginName) from local
    # storage. Nextcloud authenticates app passwords against the loginName,
    # which differs from the UID for OIDC-provisioned users; authenticate as
    # the loginName while keeping the UID for DAV/API path construction. Falls
    # back to the UID for legacy rows stored without a loginName.
    app_data = await storage.get_app_password_with_scopes(user_id)

    if not app_data:
        raise NotProvisionedError(
            f"User {user_id} has not provisioned an app password. "
            f"User must configure background sync in Astrolabe personal settings."
        )

    app_password = app_data["app_password"]
    login_name = app_data.get("username") or user_id

    logger.info("Using app password for background sync: %s", user_id)
    return NextcloudClient(
        base_url=nextcloud_host,
        username=user_id,
        auth_username=login_name,
        auth=BasicAuth(login_name, app_password),
        password=app_password,
    )


async def user_scanner_task(
    user_id: str,
    send_stream: TaskProducer,
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    nextcloud_host: str,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Scanner task for a single user.

    Gets fresh credentials at the start of each scan cycle.

    Args:
        user_id: User to scan
        send_stream: Stream to send changed documents to processors
        shutdown_event: Event signaling shutdown
        wake_event: Event to trigger immediate scan
        nextcloud_host: Nextcloud base URL
        task_status: Status object for signaling task readiness
    """
    logger.info("[BasicAuth] Scanner started for user: %s", user_id)
    settings = get_settings()
    max_consecutive_errors = 5

    task_status.started()

    # Pre-validate credentials before entering scan loop
    try:
        nc_client = await get_user_client_basic_auth(user_id, nextcloud_host)
        try:
            await nc_client.capabilities()  # Lightweight OCS call to validate creds
            logger.info("[BasicAuth] Credentials validated for %s", user_id)
        except HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.warning(
                    "[BasicAuth] Credential validation failed for %s (HTTP %s), not starting scan loop",
                    user_id,
                    e.response.status_code,
                )
                return
            raise
        finally:
            await nc_client.close()
    except NotProvisionedError:
        logger.warning(
            "[BasicAuth] User %s not provisioned, not starting scan loop", user_id
        )
        return
    except Exception as e:
        logger.warning(
            "[BasicAuth] Pre-validation failed for %s: %s. Proceeding to scan loop (has its own error handling).",
            user_id,
            e,
        )

    consecutive_errors = 0

    while not shutdown_event.is_set():
        nc_client = None
        try:
            # Get fresh credentials for this scan cycle
            nc_client = await get_user_client_basic_auth(user_id, nextcloud_host)

            # Scan user's documents
            await scan_user_documents(
                user_id=user_id,
                send_stream=send_stream,
                nc_client=nc_client,
            )

            consecutive_errors = 0  # Reset on success

        except NotProvisionedError:
            logger.warning(
                "[BasicAuth] User %s no longer provisioned, stopping scanner", user_id
            )
            break

        except HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                logger.warning(
                    "[BasicAuth] Scanner auth failed for %s (HTTP %s), stopping scanner. User may need to re-provision credentials.",
                    user_id,
                    status_code,
                )
                break
            elif status_code == 429:
                retry_after = min(int(e.response.headers.get("Retry-After", "60")), 300)
                logger.warning(
                    "[BasicAuth] Scanner rate-limited for %s, backing off %ss",
                    user_id,
                    retry_after,
                )
                try:
                    with anyio.move_on_after(retry_after):
                        await shutdown_event.wait()
                # anyio.get_cancelled_exc_class() catches task cancellation
                # (e.g. from task group teardown) so we exit cleanly.
                except anyio.get_cancelled_exc_class():
                    break
                continue
            else:
                consecutive_errors += 1
                logger.error(
                    "[BasicAuth] Scanner HTTP error for %s: %s (%s/%s)",
                    user_id,
                    e,
                    consecutive_errors,
                    max_consecutive_errors,
                    exc_info=True,
                )

        except Exception as e:
            consecutive_errors += 1
            logger.error(
                "[BasicAuth] Scanner error for %s: %s (%s/%s)",
                user_id,
                format_exception_group(e),
                consecutive_errors,
                max_consecutive_errors,
                exc_info=True,
            )

        finally:
            if nc_client:
                await nc_client.close()

        if consecutive_errors >= max_consecutive_errors:
            logger.error(
                "[BasicAuth] Scanner for %s hit %s consecutive errors, stopping scanner",
                user_id,
                max_consecutive_errors,
            )
            break

        # Sleep until next interval or wake event
        try:
            with anyio.move_on_after(settings.vector_sync_scan_interval):
                await wake_event.wait()
        except anyio.get_cancelled_exc_class():
            break

    logger.info("[BasicAuth] Scanner stopped for user: %s", user_id)


async def multi_user_processor_task(
    worker_id: int,
    receive_stream: MemoryObjectReceiveStream[DocumentTask],
    shutdown_event: anyio.Event,
    nextcloud_host: str,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Processor task for multi-user mode.

    Handles documents from any user by fetching credentials on-demand.

    Args:
        worker_id: Worker identifier for logging
        receive_stream: Stream to receive documents from
        shutdown_event: Event signaling shutdown
        nextcloud_host: Nextcloud base URL
        task_status: Status object for signaling task readiness
    """
    logger.info("[BasicAuth] Processor %s started", worker_id)
    task_status.started()

    while not shutdown_event.is_set():
        doc_task = None
        nc_client = None
        try:
            # Get document with timeout
            with anyio.fail_after(1.0):
                doc_task = await receive_stream.receive()

            # Get credentials for THIS document's user
            nc_client = await get_user_client_basic_auth(
                doc_task.user_id, nextcloud_host
            )

            # Process the document
            await process_document(doc_task, nc_client)

        except TimeoutError:
            continue

        except anyio.EndOfStream:
            logger.info("[BasicAuth] Processor %s: Stream closed, exiting", worker_id)
            break

        except NotProvisionedError:
            if doc_task is not None:
                logger.warning(
                    "[BasicAuth] User %s not provisioned, skipping %s_%s",
                    doc_task.user_id,
                    doc_task.doc_type,
                    doc_task.doc_id,
                )
            continue

        except Exception as e:
            if doc_task is not None:
                logger.error(
                    "[BasicAuth] Processor %s error processing %s_%s: %s",
                    worker_id,
                    doc_task.doc_type,
                    doc_task.doc_id,
                    format_exception_group(e),
                    exc_info=True,
                )
            else:
                logger.error(
                    "[BasicAuth] Processor %s error: %s",
                    worker_id,
                    format_exception_group(e),
                    exc_info=True,
                )

        finally:
            if nc_client:
                await nc_client.close()

    logger.info("[BasicAuth] Processor %s stopped", worker_id)


# Backward compatibility alias
oauth_processor_task = multi_user_processor_task


async def _run_user_scanner_with_scope(
    user_id: str,
    cancel_scope: anyio.CancelScope,
    send_stream: TaskProducer,
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    nextcloud_host: str,
    user_states: dict[str, UserSyncState],
) -> None:
    """Wrapper to run scanner with cancellation scope.

    Cleans up user state on exit.
    """
    cloned_stream = send_stream.clone()
    try:
        with cancel_scope:
            await user_scanner_task(
                user_id=user_id,
                send_stream=cloned_stream,
                shutdown_event=shutdown_event,
                wake_event=wake_event,
                nextcloud_host=nextcloud_host,
            )
    finally:
        # Clean up on exit
        if user_id in user_states:
            del user_states[user_id]
        await cloned_stream.aclose()


async def user_manager_task(
    send_stream: TaskProducer,
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    refresh_token_storage: "RefreshTokenStorage",
    nextcloud_host: str,
    user_states: dict[str, UserSyncState],
    tg: TaskGroup,
    provision_signal: "ProvisionSignal",
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Supervisor task that manages per-user scanners.

    Periodically polls storage to detect:
    - New users who have provisioned access -> start scanner
    - Users who have revoked access -> cancel their scanner

    Polls every ``VECTOR_SYNC_USER_POLL_INTERVAL`` seconds, but also wakes
    early whenever ``provision_signal`` is rung (by a provisioning request via
    ``notify_user_provisioned``) so a just-provisioned user's scanner starts at
    once rather than after up to a full poll interval. The poll remains the
    backstop for any missed ring.

    Args:
        send_stream: Stream to send documents to processors
        shutdown_event: Event signaling shutdown
        wake_event: Event to wake scanners for immediate scan
        refresh_token_storage: Storage for tracking provisioned users
        nextcloud_host: Nextcloud base URL
        user_states: Shared dict tracking active user scanners
        tg: Task group for spawning scanner tasks
        provision_signal: Doorbell rung on provisioning to force an early re-poll
        task_status: Status object for signaling task readiness
    """
    settings = get_settings()
    poll_interval = settings.vector_sync_user_poll_interval

    logger.info("[BasicAuth] User manager started (poll interval: %ss)", poll_interval)
    task_status.started()

    # Sleep helper: await one of the wakeup events, then end the sleep by
    # cancelling the shared scope. Defined once (not per loop iteration).
    async def _wake_on(
        wait_fn: Callable[[], Awaitable[object]], scope: anyio.CancelScope
    ) -> None:
        await wait_fn()
        scope.cancel()

    while not shutdown_event.is_set():
        try:
            # Query the app_passwords table — background sync always
            # authenticates as the user via locally-stored Nextcloud app
            # passwords (Login Flow v2 / multi-user BasicAuth).
            provisioned_users = set(
                await refresh_token_storage.get_all_app_password_user_ids()
            )
            active_users = set(user_states.keys())

            # Start scanners for new users
            new_users = provisioned_users - active_users
            for user_id in new_users:
                logger.info(
                    "[BasicAuth] Starting scanner for newly provisioned user: %s",
                    user_id,
                )
                cancel_scope = anyio.CancelScope()
                user_states[user_id] = UserSyncState(
                    user_id=user_id,
                    cancel_scope=cancel_scope,
                )

                # Start scanner in task group
                tg.start_soon(
                    _run_user_scanner_with_scope,
                    user_id,
                    cancel_scope,
                    send_stream,
                    shutdown_event,
                    wake_event,
                    nextcloud_host,
                    user_states,
                )

            # Cancel scanners for revoked users
            revoked_users = active_users - provisioned_users
            for user_id in revoked_users:
                logger.info(
                    "[BasicAuth] Stopping scanner for revoked user: %s", user_id
                )
                state = user_states.get(user_id)
                if state:
                    state.cancel_scope.cancel()
                    # Note: state will be removed by _run_user_scanner_with_scope on exit

            if new_users:
                logger.info("[BasicAuth] Started %s new scanner(s)", len(new_users))
            if revoked_users:
                logger.info("[BasicAuth] Stopped %s scanner(s)", len(revoked_users))

        except Exception as e:
            logger.error(
                "[BasicAuth] User manager error: %s",
                format_exception_group(e),
                exc_info=True,
            )

        # Sleep until the next poll tick, but wake early on shutdown or a
        # provisioning signal so a just-provisioned user is discovered at once.
        # Watch shutdown concurrently and block here on a provisioning ring;
        # whichever fires first cancels the shared scope and ends the sleep,
        # while move_on_after caps the wait at poll_interval. Awaiting one waiter
        # directly keeps an explicit checkpoint inside the cancellation scope.
        try:
            with anyio.move_on_after(poll_interval):
                async with anyio.create_task_group() as wake_tg:
                    wake_tg.start_soon(
                        _wake_on, shutdown_event.wait, wake_tg.cancel_scope
                    )
                    await _wake_on(provision_signal.wait, wake_tg.cancel_scope)
        except anyio.get_cancelled_exc_class():
            break

    # Cancel all remaining scanners on shutdown
    logger.info(
        "[BasicAuth] User manager shutting down, cancelling %s scanner(s)",
        len(user_states),
    )
    for state in list(user_states.values()):
        state.cancel_scope.cancel()

    logger.info("[BasicAuth] User manager stopped")
