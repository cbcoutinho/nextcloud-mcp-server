"""OAuth mode vector sync orchestration.

Manages multi-user background vector sync when running in OAuth mode
with ENABLE_OFFLINE_ACCESS=true:
- User Manager: Monitors RefreshTokenStorage for user changes
- Per-User Scanners: One scanner task per provisioned user
- Shared Processor Pool: Processes documents from all users
"""

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
from anyio.abc import TaskGroup, TaskStatus
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.scanner import DocumentTask, scan_user_documents

if TYPE_CHECKING:
    from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
    from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

logger = logging.getLogger(__name__)

# Scopes required for vector sync operations
VECTOR_SYNC_SCOPES = [
    "notes:read",
    "files:read",
    "deck:read",
    # "news:read",  # News app may not be installed
]


class NotProvisionedError(Exception):
    """User has not provisioned offline access or has revoked it."""

    pass


@dataclass
class UserSyncState:
    """State for a single user's scanner task."""

    user_id: str
    cancel_scope: anyio.CancelScope
    started_at: float = field(default_factory=time.time)


async def get_user_client(
    user_id: str,
    token_broker: "TokenBrokerService",
    nextcloud_host: str,
) -> NextcloudClient:
    """Get an authenticated NextcloudClient for a user.

    Args:
        user_id: User identifier
        token_broker: Token broker for obtaining access tokens
        nextcloud_host: Nextcloud base URL

    Returns:
        Authenticated NextcloudClient

    Raises:
        NotProvisionedError: If user has not provisioned offline access
    """
    token = await token_broker.get_background_token(user_id, VECTOR_SYNC_SCOPES)
    if not token:
        raise NotProvisionedError(f"User {user_id} has not provisioned offline access")

    return NextcloudClient.from_token(
        base_url=nextcloud_host,
        token=token,
        username=user_id,
    )


async def user_scanner_task(
    user_id: str,
    send_stream: MemoryObjectSendStream[DocumentTask],
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    token_broker: "TokenBrokerService",
    nextcloud_host: str,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Scanner task for a single user in OAuth mode.

    Gets a fresh token at the start of each scan cycle.

    Args:
        user_id: User to scan
        send_stream: Stream to send changed documents to processors
        shutdown_event: Event signaling shutdown
        wake_event: Event to trigger immediate scan
        token_broker: Token broker for obtaining access tokens
        nextcloud_host: Nextcloud base URL
        task_status: Status object for signaling task readiness
    """
    logger.info(f"[OAuth] Scanner started for user: {user_id}")
    settings = get_settings()

    task_status.started()

    while not shutdown_event.is_set():
        nc_client = None
        try:
            # Get fresh token for this scan cycle
            nc_client = await get_user_client(user_id, token_broker, nextcloud_host)

            # Scan user's documents
            await scan_user_documents(
                user_id=user_id,
                send_stream=send_stream,
                nc_client=nc_client,
            )

        except NotProvisionedError:
            logger.warning(
                f"[OAuth] User {user_id} no longer provisioned, stopping scanner"
            )
            break

        except Exception as e:
            logger.error(f"[OAuth] Scanner error for {user_id}: {e}", exc_info=True)

        finally:
            if nc_client:
                await nc_client.close()

        # Sleep until next interval or wake event
        try:
            with anyio.move_on_after(settings.vector_sync_scan_interval):
                await wake_event.wait()
        except anyio.get_cancelled_exc_class():
            break

    logger.info(f"[OAuth] Scanner stopped for user: {user_id}")


async def oauth_processor_task(
    worker_id: int,
    receive_stream: MemoryObjectReceiveStream[DocumentTask],
    shutdown_event: anyio.Event,
    token_broker: "TokenBrokerService",
    nextcloud_host: str,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Processor task for OAuth mode.

    Handles documents from any user by fetching tokens on-demand.

    Args:
        worker_id: Worker identifier for logging
        receive_stream: Stream to receive documents from
        shutdown_event: Event signaling shutdown
        token_broker: Token broker for obtaining access tokens
        nextcloud_host: Nextcloud base URL
        task_status: Status object for signaling task readiness
    """
    from nextcloud_mcp_server.vector.processor import process_document

    logger.info(f"[OAuth] Processor {worker_id} started")
    task_status.started()

    while not shutdown_event.is_set():
        doc_task = None
        nc_client = None
        try:
            # Get document with timeout
            with anyio.fail_after(1.0):
                doc_task = await receive_stream.receive()

            # Get token for THIS document's user
            nc_client = await get_user_client(
                doc_task.user_id, token_broker, nextcloud_host
            )

            # Process the document
            await process_document(doc_task, nc_client)

        except TimeoutError:
            continue

        except anyio.EndOfStream:
            logger.info(f"[OAuth] Processor {worker_id}: Stream closed, exiting")
            break

        except NotProvisionedError:
            if doc_task:
                logger.warning(
                    f"[OAuth] User {doc_task.user_id} not provisioned, "
                    f"skipping {doc_task.doc_type}_{doc_task.doc_id}"
                )
            continue

        except Exception as e:
            if doc_task:
                logger.error(
                    f"[OAuth] Processor {worker_id} error processing "
                    f"{doc_task.doc_type}_{doc_task.doc_id}: {e}",
                    exc_info=True,
                )
            else:
                logger.error(f"[OAuth] Processor {worker_id} error: {e}", exc_info=True)

        finally:
            if nc_client:
                await nc_client.close()

    logger.info(f"[OAuth] Processor {worker_id} stopped")


async def _run_user_scanner_with_scope(
    user_id: str,
    cancel_scope: anyio.CancelScope,
    send_stream: MemoryObjectSendStream[DocumentTask],
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    token_broker: "TokenBrokerService",
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
                token_broker=token_broker,
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
    token_broker: "TokenBrokerService",
    refresh_token_storage: "RefreshTokenStorage",
    nextcloud_host: str,
    user_states: dict[str, UserSyncState],
    tg: TaskGroup,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Supervisor task that manages per-user scanners.

    Periodically polls RefreshTokenStorage to detect:
    - New users who have provisioned offline access -> start scanner
    - Users who have revoked access -> cancel their scanner

    Args:
        send_stream: Stream to send documents to processors
        shutdown_event: Event signaling shutdown
        wake_event: Event to wake scanners for immediate scan
        token_broker: Token broker for obtaining access tokens
        refresh_token_storage: Storage for refresh tokens
        nextcloud_host: Nextcloud base URL
        user_states: Shared dict tracking active user scanners
        tg: Task group for spawning scanner tasks
        task_status: Status object for signaling task readiness
    """
    settings = get_settings()
    poll_interval = settings.vector_sync_user_poll_interval

    logger.info(f"[OAuth] User manager started (poll interval: {poll_interval}s)")
    task_status.started()

    while not shutdown_event.is_set():
        try:
            # Get current provisioned users
            provisioned_users = set(await refresh_token_storage.get_all_user_ids())
            active_users = set(user_states.keys())

            # Start scanners for new users
            new_users = provisioned_users - active_users
            for user_id in new_users:
                logger.info(
                    f"[OAuth] Starting scanner for newly provisioned user: {user_id}"
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
                    token_broker,
                    nextcloud_host,
                    user_states,
                )

            # Cancel scanners for revoked users
            revoked_users = active_users - provisioned_users
            for user_id in revoked_users:
                logger.info(f"[OAuth] Stopping scanner for revoked user: {user_id}")
                state = user_states.get(user_id)
                if state:
                    state.cancel_scope.cancel()
                    # Note: state will be removed by _run_user_scanner_with_scope on exit

            if new_users:
                logger.info(f"[OAuth] Started {len(new_users)} new scanner(s)")
            if revoked_users:
                logger.info(f"[OAuth] Stopped {len(revoked_users)} scanner(s)")

        except Exception as e:
            logger.error(f"[OAuth] User manager error: {e}", exc_info=True)

        # Sleep until next poll
        try:
            with anyio.move_on_after(poll_interval):
                await shutdown_event.wait()
        except anyio.get_cancelled_exc_class():
            break

    # Cancel all remaining scanners on shutdown
    logger.info(
        f"[OAuth] User manager shutting down, cancelling {len(user_states)} scanner(s)"
    )
    for state in list(user_states.values()):
        state.cancel_scope.cancel()

    logger.info("[OAuth] User manager stopped")
