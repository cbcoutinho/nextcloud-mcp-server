"""Unit tests: self-healing removal of stale app passwords (Deck #198).

When a Nextcloud user is deleted/disabled, their stored app password keeps
returning 401/403. Previously the scanner just stopped, leaving the credential in
storage so ``user_manager_task`` re-spawned the scanner every poll interval — an
endless re-spawn/401 loop. The scanner now deletes the credential on a hard auth
failure, and a periodic ``credential_cleanup_task`` sweeps any that slip through.
"""

import logging

import anyio
import httpx
import pytest

from nextcloud_mcp_server.vector import oauth_sync

pytestmark = pytest.mark.unit


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://cloud.example.org/ocs")
    return httpx.HTTPStatusError(
        "auth failure", request=req, response=httpx.Response(status, request=req)
    )


async def test_remove_stale_credential_deletes(mocker):
    """The helper deletes the user's app password via storage."""
    storage = mocker.MagicMock()
    storage.delete_app_password = mocker.AsyncMock(return_value=True)
    mocker.patch.object(
        oauth_sync,
        "_get_initialized_basic_auth_storage",
        mocker.AsyncMock(return_value=storage),
    )

    await oauth_sync._remove_stale_credential("ghost-user", 401)

    storage.delete_app_password.assert_awaited_once_with("ghost-user")


async def test_remove_stale_credential_swallows_storage_error(mocker):
    """Best-effort: a storage failure is logged, never raised (backstops cover it)."""
    storage = mocker.MagicMock()
    storage.delete_app_password = mocker.AsyncMock(side_effect=RuntimeError("db down"))
    mocker.patch.object(
        oauth_sync,
        "_get_initialized_basic_auth_storage",
        mocker.AsyncMock(return_value=storage),
    )

    # Must not raise.
    await oauth_sync._remove_stale_credential("ghost-user", 401)


@pytest.mark.parametrize("status", [401, 403])
async def test_scanner_removes_credential_on_prevalidation_auth_failure(mocker, status):
    """A 401/403 validating creds deletes the credential and never enters the scan
    loop — so ``user_manager_task`` won't see the user as provisioned again."""
    fake_client = mocker.AsyncMock()
    fake_client.capabilities = mocker.AsyncMock(side_effect=_http_error(status))
    fake_client.close = mocker.AsyncMock()
    mocker.patch.object(
        oauth_sync,
        "get_user_client_basic_auth",
        mocker.AsyncMock(return_value=fake_client),
    )
    storage = mocker.MagicMock()
    storage.delete_app_password = mocker.AsyncMock(return_value=True)
    mocker.patch.object(
        oauth_sync,
        "_get_initialized_basic_auth_storage",
        mocker.AsyncMock(return_value=storage),
    )

    await oauth_sync.user_scanner_task(
        "ghost-user",
        mocker.MagicMock(),  # send_stream — unused on the pre-validation path
        anyio.Event(),  # shutdown_event
        anyio.Event(),  # wake_event
        "https://cloud.example.org",
    )

    storage.delete_app_password.assert_awaited_once_with("ghost-user")


@pytest.mark.parametrize("status", [401, 403])
async def test_scanner_removes_credential_on_scan_loop_auth_failure(mocker, status):
    """A 401/403 raised while scanning (not pre-validation) also deletes the
    credential before the scanner stops."""
    fake_client = mocker.AsyncMock()
    fake_client.capabilities = mocker.AsyncMock(return_value={})  # pre-validation ok
    fake_client.close = mocker.AsyncMock()
    mocker.patch.object(
        oauth_sync,
        "get_user_client_basic_auth",
        mocker.AsyncMock(return_value=fake_client),
    )
    mocker.patch.object(
        oauth_sync,
        "scan_user_documents",
        mocker.AsyncMock(side_effect=_http_error(status)),
    )
    storage = mocker.MagicMock()
    storage.delete_app_password = mocker.AsyncMock(return_value=True)
    mocker.patch.object(
        oauth_sync,
        "_get_initialized_basic_auth_storage",
        mocker.AsyncMock(return_value=storage),
    )

    await oauth_sync.user_scanner_task(
        "ghost-user",
        mocker.MagicMock(),
        anyio.Event(),
        anyio.Event(),
        "https://cloud.example.org",
    )

    storage.delete_app_password.assert_awaited_once_with("ghost-user")


async def test_credential_cleanup_task_sweeps_then_stops(mocker):
    """The periodic backstop validates stored passwords via
    ``cleanup_invalid_app_passwords`` and exits on shutdown."""
    mocker.patch.object(oauth_sync, "CREDENTIAL_CLEANUP_INTERVAL", 0)
    shutdown = anyio.Event()
    storage = mocker.MagicMock()

    async def _cleanup(host):
        shutdown.set()  # stop the loop after the first sweep
        return ["ghost-user"]

    storage.cleanup_invalid_app_passwords = mocker.AsyncMock(side_effect=_cleanup)

    await oauth_sync.credential_cleanup_task(
        storage, shutdown, "https://cloud.example.org"
    )

    storage.cleanup_invalid_app_passwords.assert_awaited_once_with(
        "https://cloud.example.org"
    )


async def test_credential_cleanup_task_swallows_sweep_exception(mocker, caplog):
    """A failing sweep is logged non-fatally and does not crash the task — the
    loop survives to retry on the next cadence."""
    mocker.patch.object(oauth_sync, "CREDENTIAL_CLEANUP_INTERVAL", 0)
    shutdown = anyio.Event()
    storage = mocker.MagicMock()

    async def _boom(host):
        shutdown.set()  # exit after this (failed) iteration
        raise RuntimeError("sweep failed")

    storage.cleanup_invalid_app_passwords = mocker.AsyncMock(side_effect=_boom)

    with caplog.at_level(
        logging.WARNING, logger="nextcloud_mcp_server.vector.oauth_sync"
    ):
        # Must return normally — the exception is swallowed, not propagated.
        await oauth_sync.credential_cleanup_task(
            storage, shutdown, "https://cloud.example.org"
        )

    storage.cleanup_invalid_app_passwords.assert_awaited_once()
    assert "non-fatal" in caplog.text
