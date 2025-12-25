"""Multi-user vector sync orchestration.

Manages background vector sync for multi-user deployments:
- User Manager: Monitors storage for user changes
- Per-User Scanners: One scanner task per provisioned user
- Shared Processor Pool: Processes documents from all users

Authentication strategies are mutually exclusive by deployment mode:

Multi-user BasicAuth mode (ENABLE_MULTI_USER_BASIC_AUTH=true):
- Uses app passwords obtained via Astrolabe Management API
- Users provision via Astrolabe personal settings
- OAuth is NOT used

OAuth mode (with external IdP like Keycloak):
- Uses OAuth refresh tokens via TokenBrokerService
- Users provision via browser OAuth flow
- App passwords are NOT used

These are separate concerns - no fallback between them.
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
from httpx import BasicAuth

from nextcloud_mcp_server.auth.astrolabe_client import AstrolabeClient
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


async def get_user_client_basic_auth(
    user_id: str,
    nextcloud_host: str,
) -> NextcloudClient:
    """Get an authenticated NextcloudClient using app password (BasicAuth mode).

    For multi-user BasicAuth deployments where users provision app passwords
    via Astrolabe personal settings. OAuth is NOT used in this mode.

    Args:
        user_id: User identifier
        nextcloud_host: Nextcloud base URL

    Returns:
        Authenticated NextcloudClient with BasicAuth

    Raises:
        NotProvisionedError: If user has not provisioned an app password
    """
    settings = get_settings()

    if not settings.oidc_client_id or not settings.oidc_client_secret:
        raise NotProvisionedError(
            "Astrolabe client credentials not configured. "
            "Set OIDC_CLIENT_ID and OIDC_CLIENT_SECRET for app password retrieval."
        )

    astrolabe = AstrolabeClient(
        nextcloud_host=nextcloud_host,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
    )

    app_password = await astrolabe.get_user_app_password(user_id)

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
    )


async def get_user_client_oauth(
    user_id: str,
    token_broker: "TokenBrokerService",
    nextcloud_host: str,
) -> NextcloudClient:
    """Get an authenticated NextcloudClient using OAuth refresh token.

    For OAuth deployments with external IdP where users provision via
    browser OAuth flow. App passwords are NOT used in this mode.

    Args:
        user_id: User identifier
        token_broker: Token broker for obtaining access tokens
        nextcloud_host: Nextcloud base URL

    Returns:
        Authenticated NextcloudClient with Bearer token

    Raises:
        NotProvisionedError: If user has not provisioned offline access
    """
    token = await token_broker.get_background_token(user_id, VECTOR_SYNC_SCOPES)
    if not token:
        raise NotProvisionedError(
            f"User {user_id} has not provisioned offline access. "
            f"User must complete the OAuth provisioning flow."
        )

    logger.info(f"Using OAuth refresh token for background sync: {user_id}")
    return NextcloudClient.from_token(
        base_url=nextcloud_host,
        token=token,
        username=user_id,
    )


async def get_user_client(
    user_id: str,
    token_broker: "TokenBrokerService | None",
    nextcloud_host: str,
    *,
    use_basic_auth: bool = False,
) -> NextcloudClient:
    """Get an authenticated NextcloudClient for a user.

    Dispatches to the appropriate authentication strategy based on mode.
    These are mutually exclusive - no fallback between them.

    Args:
        user_id: User identifier
        token_broker: Token broker for OAuth mode (can be None for BasicAuth mode)
        nextcloud_host: Nextcloud base URL
        use_basic_auth: If True, use app passwords via Astrolabe (BasicAuth mode).
                       If False, use OAuth refresh tokens (OAuth mode).

    Returns:
        Authenticated NextcloudClient

    Raises:
        NotProvisionedError: If user has not provisioned access for the mode
    """
    if use_basic_auth:
        return await get_user_client_basic_auth(user_id, nextcloud_host)
    else:
        if token_broker is None:
            raise ValueError("token_broker required for OAuth mode")
        return await get_user_client_oauth(user_id, token_broker, nextcloud_host)


async def user_scanner_task(
    user_id: str,
    send_stream: MemoryObjectSendStream[DocumentTask],
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    token_broker: "TokenBrokerService | None",
    nextcloud_host: str,
    *,
    use_basic_auth: bool = False,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Scanner task for a single user.

    Gets fresh credentials at the start of each scan cycle.

    Args:
        user_id: User to scan
        send_stream: Stream to send changed documents to processors
        shutdown_event: Event signaling shutdown
        wake_event: Event to trigger immediate scan
        token_broker: Token broker for OAuth mode (None for BasicAuth mode)
        nextcloud_host: Nextcloud base URL
        use_basic_auth: If True, use app passwords; if False, use OAuth tokens
        task_status: Status object for signaling task readiness
    """
    mode_label = "BasicAuth" if use_basic_auth else "OAuth"
    logger.info(f"[{mode_label}] Scanner started for user: {user_id}")
    settings = get_settings()

    task_status.started()

    while not shutdown_event.is_set():
        nc_client = None
        try:
            # Get fresh credentials for this scan cycle
            nc_client = await get_user_client(
                user_id, token_broker, nextcloud_host, use_basic_auth=use_basic_auth
            )

            # Scan user's documents
            await scan_user_documents(
                user_id=user_id,
                send_stream=send_stream,
                nc_client=nc_client,
            )

        except NotProvisionedError:
            logger.warning(
                f"[{mode_label}] User {user_id} no longer provisioned, stopping scanner"
            )
            break

        except Exception as e:
            logger.error(
                f"[{mode_label}] Scanner error for {user_id}: {e}", exc_info=True
            )

        finally:
            if nc_client:
                await nc_client.close()

        # Sleep until next interval or wake event
        try:
            with anyio.move_on_after(settings.vector_sync_scan_interval):
                await wake_event.wait()
        except anyio.get_cancelled_exc_class():
            break

    logger.info(f"[{mode_label}] Scanner stopped for user: {user_id}")


async def multi_user_processor_task(
    worker_id: int,
    receive_stream: MemoryObjectReceiveStream[DocumentTask],
    shutdown_event: anyio.Event,
    token_broker: "TokenBrokerService | None",
    nextcloud_host: str,
    use_basic_auth: bool = False,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Processor task for multi-user mode.

    Handles documents from any user by fetching credentials on-demand.

    Args:
        worker_id: Worker identifier for logging
        receive_stream: Stream to receive documents from
        shutdown_event: Event signaling shutdown
        token_broker: Token broker for OAuth mode (None for BasicAuth mode)
        nextcloud_host: Nextcloud base URL
        use_basic_auth: If True, use app passwords; if False, use OAuth tokens
        task_status: Status object for signaling task readiness
    """
    from nextcloud_mcp_server.vector.processor import process_document

    mode_label = "BasicAuth" if use_basic_auth else "OAuth"
    logger.info(f"[{mode_label}] Processor {worker_id} started")
    task_status.started()

    while not shutdown_event.is_set():
        doc_task = None
        nc_client = None
        try:
            # Get document with timeout
            with anyio.fail_after(1.0):
                doc_task = await receive_stream.receive()

            # Get credentials for THIS document's user
            nc_client = await get_user_client(
                doc_task.user_id,
                token_broker,
                nextcloud_host,
                use_basic_auth=use_basic_auth,
            )

            # Process the document
            await process_document(doc_task, nc_client)

        except TimeoutError:
            continue

        except anyio.EndOfStream:
            logger.info(f"[{mode_label}] Processor {worker_id}: Stream closed, exiting")
            break

        except NotProvisionedError:
            if doc_task:
                logger.warning(
                    f"[{mode_label}] User {doc_task.user_id} not provisioned, "
                    f"skipping {doc_task.doc_type}_{doc_task.doc_id}"
                )
            continue

        except Exception as e:
            if doc_task:
                logger.error(
                    f"[{mode_label}] Processor {worker_id} error processing "
                    f"{doc_task.doc_type}_{doc_task.doc_id}: {e}",
                    exc_info=True,
                )
            else:
                logger.error(
                    f"[{mode_label}] Processor {worker_id} error: {e}", exc_info=True
                )

        finally:
            if nc_client:
                await nc_client.close()

    logger.info(f"[{mode_label}] Processor {worker_id} stopped")


# Backward compatibility alias
oauth_processor_task = multi_user_processor_task


async def _run_user_scanner_with_scope(
    user_id: str,
    cancel_scope: anyio.CancelScope,
    send_stream: MemoryObjectSendStream[DocumentTask],
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    token_broker: "TokenBrokerService | None",
    nextcloud_host: str,
    user_states: dict[str, UserSyncState],
    use_basic_auth: bool = False,
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
                use_basic_auth=use_basic_auth,
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
    token_broker: "TokenBrokerService | None",
    refresh_token_storage: "RefreshTokenStorage",
    nextcloud_host: str,
    user_states: dict[str, UserSyncState],
    tg: TaskGroup,
    use_basic_auth: bool = False,
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
        token_broker: Token broker for OAuth mode (None for BasicAuth mode)
        refresh_token_storage: Storage for tracking provisioned users
        nextcloud_host: Nextcloud base URL
        user_states: Shared dict tracking active user scanners
        tg: Task group for spawning scanner tasks
        use_basic_auth: If True, use app passwords; if False, use OAuth tokens
        task_status: Status object for signaling task readiness
    """
    settings = get_settings()
    poll_interval = settings.vector_sync_user_poll_interval
    mode_label = "BasicAuth" if use_basic_auth else "OAuth"

    logger.info(
        f"[{mode_label}] User manager started (poll interval: {poll_interval}s)"
    )
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
                    f"[{mode_label}] Starting scanner for newly provisioned user: {user_id}"
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
                    use_basic_auth,  # Positional after user_states
                )

            # Cancel scanners for revoked users
            revoked_users = active_users - provisioned_users
            for user_id in revoked_users:
                logger.info(
                    f"[{mode_label}] Stopping scanner for revoked user: {user_id}"
                )
                state = user_states.get(user_id)
                if state:
                    state.cancel_scope.cancel()
                    # Note: state will be removed by _run_user_scanner_with_scope on exit

            if new_users:
                logger.info(f"[{mode_label}] Started {len(new_users)} new scanner(s)")
            if revoked_users:
                logger.info(f"[{mode_label}] Stopped {len(revoked_users)} scanner(s)")

        except Exception as e:
            logger.error(f"[{mode_label}] User manager error: {e}", exc_info=True)

        # Sleep until next poll
        try:
            with anyio.move_on_after(poll_interval):
                await shutdown_event.wait()
        except anyio.get_cancelled_exc_class():
            break

    # Cancel all remaining scanners on shutdown
    logger.info(
        f"[{mode_label}] User manager shutting down, cancelling {len(user_states)} scanner(s)"
    )
    for state in list(user_states.values()):
        state.cancel_scope.cancel()

    logger.info(f"[{mode_label}] User manager stopped")
