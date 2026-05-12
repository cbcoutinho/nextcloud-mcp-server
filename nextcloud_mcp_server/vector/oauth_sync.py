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
from dataclasses import dataclass, field

import anyio
from anyio.abc import TaskGroup, TaskStatus
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from httpx import BasicAuth, HTTPStatusError

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.processor import process_document
from nextcloud_mcp_server.vector.scanner import DocumentTask, scan_user_documents

logger = logging.getLogger(__name__)


class NotProvisionedError(Exception):
    """User has not provisioned offline access or has revoked it."""

    pass


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
    # Get or create storage instance
    if storage is None:
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

    # Retrieve app password from local storage
    app_password = await storage.get_app_password(user_id)

    if not app_password:
        raise NotProvisionedError(
            f"User {user_id} has not provisioned an app password. "
            f"User must configure background sync in Astrolabe personal settings."
        )

    logger.info(f"Using app password for background sync: {user_id}")
    return NextcloudClient(
        base_url=nextcloud_host,
        username=user_id,
        auth=BasicAuth(user_id, app_password),
        password=app_password,
    )


async def user_scanner_task(
    user_id: str,
    send_stream: MemoryObjectSendStream[DocumentTask],
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
    logger.info(f"[BasicAuth] Scanner started for user: {user_id}")
    settings = get_settings()
    max_consecutive_errors = 5

    task_status.started()

    # Pre-validate credentials before entering scan loop
    try:
        nc_client = await get_user_client_basic_auth(user_id, nextcloud_host)
        try:
            await nc_client.capabilities()  # Lightweight OCS call to validate creds
            logger.info(f"[BasicAuth] Credentials validated for {user_id}")
        except HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.warning(
                    f"[BasicAuth] Credential validation failed for {user_id} "
                    f"(HTTP {e.response.status_code}), not starting scan loop"
                )
                return
            raise
        finally:
            await nc_client.close()
    except NotProvisionedError:
        logger.warning(
            f"[BasicAuth] User {user_id} not provisioned, not starting scan loop"
        )
        return
    except Exception as e:
        logger.warning(
            f"[BasicAuth] Pre-validation failed for {user_id}: {e}. "
            f"Proceeding to scan loop (has its own error handling)."
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
                f"[BasicAuth] User {user_id} no longer provisioned, stopping scanner"
            )
            break

        except HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                logger.warning(
                    f"[BasicAuth] Scanner auth failed for {user_id} "
                    f"(HTTP {status_code}), stopping scanner. "
                    f"User may need to re-provision credentials."
                )
                break
            elif status_code == 429:
                retry_after = min(int(e.response.headers.get("Retry-After", "60")), 300)
                logger.warning(
                    f"[BasicAuth] Scanner rate-limited for {user_id}, "
                    f"backing off {retry_after}s"
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
                    f"[BasicAuth] Scanner HTTP error for {user_id}: {e} "
                    f"({consecutive_errors}/{max_consecutive_errors})",
                    exc_info=True,
                )

        except Exception as e:
            consecutive_errors += 1
            logger.error(
                f"[BasicAuth] Scanner error for {user_id}: {e} "
                f"({consecutive_errors}/{max_consecutive_errors})",
                exc_info=True,
            )

        finally:
            if nc_client:
                await nc_client.close()

        if consecutive_errors >= max_consecutive_errors:
            logger.error(
                f"[BasicAuth] Scanner for {user_id} hit {max_consecutive_errors} "
                f"consecutive errors, stopping scanner"
            )
            break

        # Sleep until next interval or wake event
        try:
            with anyio.move_on_after(settings.vector_sync_scan_interval):
                await wake_event.wait()
        except anyio.get_cancelled_exc_class():
            break

    logger.info(f"[BasicAuth] Scanner stopped for user: {user_id}")


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
    logger.info(f"[BasicAuth] Processor {worker_id} started")
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
            logger.info(f"[BasicAuth] Processor {worker_id}: Stream closed, exiting")
            break

        except NotProvisionedError:
            if doc_task:
                logger.warning(
                    f"[BasicAuth] User {doc_task.user_id} not provisioned, "
                    f"skipping {doc_task.doc_type}_{doc_task.doc_id}"
                )
            continue

        except Exception as e:
            if doc_task:
                logger.error(
                    f"[BasicAuth] Processor {worker_id} error processing "
                    f"{doc_task.doc_type}_{doc_task.doc_id}: {e}",
                    exc_info=True,
                )
            else:
                logger.error(
                    f"[BasicAuth] Processor {worker_id} error: {e}", exc_info=True
                )

        finally:
            if nc_client:
                await nc_client.close()

    logger.info(f"[BasicAuth] Processor {worker_id} stopped")


# Backward compatibility alias
oauth_processor_task = multi_user_processor_task


async def _run_user_scanner_with_scope(
    user_id: str,
    cancel_scope: anyio.CancelScope,
    send_stream: MemoryObjectSendStream[DocumentTask],
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
    send_stream: MemoryObjectSendStream[DocumentTask],
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    refresh_token_storage: "RefreshTokenStorage",
    nextcloud_host: str,
    user_states: dict[str, UserSyncState],
    tg: TaskGroup,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Supervisor task that manages per-user scanners.

    Periodically polls storage to detect:
    - New users who have provisioned access -> start scanner
    - Users who have revoked access -> cancel their scanner

    Args:
        send_stream: Stream to send documents to processors
        shutdown_event: Event signaling shutdown
        wake_event: Event to wake scanners for immediate scan
        refresh_token_storage: Storage for tracking provisioned users
        nextcloud_host: Nextcloud base URL
        user_states: Shared dict tracking active user scanners
        tg: Task group for spawning scanner tasks
        task_status: Status object for signaling task readiness
    """
    settings = get_settings()
    poll_interval = settings.vector_sync_user_poll_interval

    logger.info(f"[BasicAuth] User manager started (poll interval: {poll_interval}s)")
    task_status.started()

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
                    f"[BasicAuth] Starting scanner for newly provisioned user: {user_id}"
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
                logger.info(f"[BasicAuth] Stopping scanner for revoked user: {user_id}")
                state = user_states.get(user_id)
                if state:
                    state.cancel_scope.cancel()
                    # Note: state will be removed by _run_user_scanner_with_scope on exit

            if new_users:
                logger.info(f"[BasicAuth] Started {len(new_users)} new scanner(s)")
            if revoked_users:
                logger.info(f"[BasicAuth] Stopped {len(revoked_users)} scanner(s)")

        except Exception as e:
            logger.error(f"[BasicAuth] User manager error: {e}", exc_info=True)

        # Sleep until next poll
        try:
            with anyio.move_on_after(poll_interval):
                await shutdown_event.wait()
        except anyio.get_cancelled_exc_class():
            break

    # Cancel all remaining scanners on shutdown
    logger.info(
        f"[BasicAuth] User manager shutting down, cancelling {len(user_states)} scanner(s)"
    )
    for state in list(user_states.values()):
        state.cancel_scope.cancel()

    logger.info("[BasicAuth] User manager stopped")
