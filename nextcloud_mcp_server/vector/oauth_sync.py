"""Multi-user vector sync orchestration.

Manages background vector sync for multi-user deployments:
- User Manager: Monitors storage for user changes
- Per-User Scanners: One scanner task per provisioned user
- Shared Processor Pool: Processes documents from all users

Authentication strategies are mutually exclusive by deployment mode:

Multi-user BasicAuth mode (MCP_DEPLOYMENT_MODE=multi_user_basic):
- Uses app passwords stored locally in MCP server's database
- Users provision via Astrolabe personal settings, which sends to MCP API
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
from httpx import BasicAuth, HTTPStatusError

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.processor import process_document
from nextcloud_mcp_server.vector.scanner import DocumentTask, scan_user_documents

if TYPE_CHECKING:
    from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

logger = logging.getLogger(__name__)

# Scopes required for vector sync operations
VECTOR_SYNC_SCOPES = [
    "notes.read",
    "files.read",
    "deck.read",
    # "news.read",  # News app may not be installed
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

    logger.info("Using app password for background sync: %s", user_id)
    return NextcloudClient(
        base_url=nextcloud_host,
        username=user_id,
        auth=BasicAuth(user_id, app_password),
        password=app_password,
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

    logger.info("Using OAuth refresh token for background sync: %s", user_id)
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
    logger.info("[%s] Scanner started for user: %s", mode_label, user_id)
    settings = get_settings()
    max_consecutive_errors = 5

    task_status.started()

    # Pre-validate credentials before entering scan loop
    try:
        nc_client = await get_user_client(
            user_id, token_broker, nextcloud_host, use_basic_auth=use_basic_auth
        )
        try:
            await nc_client.capabilities()  # Lightweight OCS call to validate creds
            logger.info("[%s] Credentials validated for %s", mode_label, user_id)
        except HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.warning(
                    "[%s] Credential validation failed for %s (HTTP %s), not starting scan loop",
                    mode_label,
                    user_id,
                    e.response.status_code,
                )
                return
            raise
        finally:
            await nc_client.close()
    except NotProvisionedError:
        logger.warning(
            "[%s] User %s not provisioned, not starting scan loop", mode_label, user_id
        )
        return
    except Exception as e:
        logger.warning(
            "[%s] Pre-validation failed for %s: %s. Proceeding to scan loop (has its own error handling).",
            mode_label,
            user_id,
            e,
        )

    consecutive_errors = 0

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

            consecutive_errors = 0  # Reset on success

        except NotProvisionedError:
            logger.warning(
                "[%s] User %s no longer provisioned, stopping scanner",
                mode_label,
                user_id,
            )
            break

        except HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                logger.warning(
                    "[%s] Scanner auth failed for %s (HTTP %s), stopping scanner. User may need to re-provision credentials.",
                    mode_label,
                    user_id,
                    status_code,
                )
                break
            elif status_code == 429:
                retry_after = min(int(e.response.headers.get("Retry-After", "60")), 300)
                logger.warning(
                    "[%s] Scanner rate-limited for %s, backing off %ss",
                    mode_label,
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
                    "[%s] Scanner HTTP error for %s: %s (%s/%s)",
                    mode_label,
                    user_id,
                    e,
                    consecutive_errors,
                    max_consecutive_errors,
                    exc_info=True,
                )

        except Exception as e:
            consecutive_errors += 1
            logger.error(
                "[%s] Scanner error for %s: %s (%s/%s)",
                mode_label,
                user_id,
                e,
                consecutive_errors,
                max_consecutive_errors,
                exc_info=True,
            )

        finally:
            if nc_client:
                await nc_client.close()

        if consecutive_errors >= max_consecutive_errors:
            logger.error(
                "[%s] Scanner for %s hit %s consecutive errors, stopping scanner",
                mode_label,
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

    logger.info("[%s] Scanner stopped for user: %s", mode_label, user_id)


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
    mode_label = "BasicAuth" if use_basic_auth else "OAuth"
    logger.info("[%s] Processor %s started", mode_label, worker_id)
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
            logger.info(
                "[%s] Processor %s: Stream closed, exiting", mode_label, worker_id
            )
            break

        except NotProvisionedError:
            if doc_task:
                logger.warning(
                    "[%s] User %s not provisioned, skipping %s_%s",
                    mode_label,
                    doc_task.user_id,
                    doc_task.doc_type,
                    doc_task.doc_id,
                )
            continue

        except Exception as e:
            if doc_task:
                logger.error(
                    "[%s] Processor %s error processing %s_%s: %s",
                    mode_label,
                    worker_id,
                    doc_task.doc_type,
                    doc_task.doc_id,
                    e,
                    exc_info=True,
                )
            else:
                logger.error(
                    "[%s] Processor %s error: %s",
                    mode_label,
                    worker_id,
                    e,
                    exc_info=True,
                )

        finally:
            if nc_client:
                await nc_client.close()

    logger.info("[%s] Processor %s stopped", mode_label, worker_id)


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
        "[%s] User manager started (poll interval: %ss)", mode_label, poll_interval
    )
    task_status.started()

    while not shutdown_event.is_set():
        try:
            # Get current provisioned users based on mode
            if use_basic_auth:
                # BasicAuth / Login Flow v2 mode: query app_passwords table
                provisioned_users = set(
                    await refresh_token_storage.get_all_app_password_user_ids()
                )
            else:
                # OAuth mode: query refresh_tokens table
                provisioned_users = set(await refresh_token_storage.get_all_user_ids())
            active_users = set(user_states.keys())

            # Start scanners for new users
            new_users = provisioned_users - active_users
            for user_id in new_users:
                logger.info(
                    "[%s] Starting scanner for newly provisioned user: %s",
                    mode_label,
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
                    token_broker,
                    nextcloud_host,
                    user_states,
                    use_basic_auth,  # Positional after user_states
                )

            # Cancel scanners for revoked users
            revoked_users = active_users - provisioned_users
            for user_id in revoked_users:
                logger.info(
                    "[%s] Stopping scanner for revoked user: %s", mode_label, user_id
                )
                state = user_states.get(user_id)
                if state:
                    state.cancel_scope.cancel()
                    # Note: state will be removed by _run_user_scanner_with_scope on exit

            if new_users:
                logger.info(
                    "[%s] Started %s new scanner(s)", mode_label, len(new_users)
                )
            if revoked_users:
                logger.info(
                    "[%s] Stopped %s scanner(s)", mode_label, len(revoked_users)
                )

        except Exception as e:
            logger.error("[%s] User manager error: %s", mode_label, e, exc_info=True)

        # Sleep until next poll
        try:
            with anyio.move_on_after(poll_interval):
                await shutdown_event.wait()
        except anyio.get_cancelled_exc_class():
            break

    # Cancel all remaining scanners on shutdown
    logger.info(
        "[%s] User manager shutting down, cancelling %s scanner(s)",
        mode_label,
        len(user_states),
    )
    for state in list(user_states.values()):
        state.cancel_scope.cancel()

    logger.info("[%s] User manager stopped", mode_label)
