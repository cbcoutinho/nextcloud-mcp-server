"""Integration test: Login Flow v2 provisioning wakes the background sync
user manager immediately.

Wires the real login-flow web-provision path end to end at the component level
(no browser / container):

    _poll_and_store (provision_routes)
      -> RefreshTokenStorage.store_app_password_with_scopes  (real, temp DB)
      -> notify_user_provisioned  ->  ProvisionSignal.ring
      -> user_manager_task wakes, re-polls the same storage, spawns the scanner

Only the Nextcloud-facing Login Flow v2 poll is mocked (returning "completed");
everything in between is the real code. With a deliberately long poll interval,
the new user's scanner must still be spawned promptly — proving the wake came
from the provisioning signal, not the periodic poll.

This is the Login Flow v2 deployment-mode counterpart to the multi-user
BasicAuth coverage in ``test_app_password_provisioning.py``.
"""

import secrets
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.login_flow import LoginFlowPollResult
from nextcloud_mcp_server.auth.provision_routes import (
    _poll_and_store,
    _provision_sessions,
)
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.vector.oauth_sync import ProvisionSignal, user_manager_task

pytestmark = pytest.mark.integration


@pytest.fixture
def encryption_key():
    return Fernet.generate_key().decode()


@pytest.fixture
async def temp_storage(encryption_key):
    """Real RefreshTokenStorage backed by a temporary SQLite DB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_login_flow_wake.db"
        storage = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=encryption_key
        )
        await storage.initialize()
        yield storage


async def test_login_flow_provision_wakes_user_manager(temp_storage, mocker):
    """A completed Login Flow v2 web provision spawns the user's scanner at
    once via the provision signal, well inside a long poll interval."""
    # ── user_manager: long poll interval + stubbed per-user scanner ──────────
    manager_settings = MagicMock()
    manager_settings.vector_sync_user_poll_interval = 1000  # never fires here
    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.get_settings",
        return_value=manager_settings,
    )

    spawned: set[str] = set()
    alice_spawned = anyio.Event()

    async def fake_scanner(
        user_id,
        cancel_scope,
        send_stream,
        shutdown_event,
        wake_event,
        nextcloud_host,
        user_states,
    ):
        spawned.add(user_id)
        if user_id == "alice":
            alice_spawned.set()
        with cancel_scope:
            await shutdown_event.wait()
        user_states.pop(user_id, None)

    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync._run_user_scanner_with_scope",
        fake_scanner,
    )

    # ── wire the doorbell exactly as the lifespan does ───────────────────────
    import nextcloud_mcp_server.app as app_module

    provision_signal = ProvisionSignal()
    mocker.patch.object(
        app_module._vector_sync_state, "provision_signal", provision_signal
    )

    # ── mock only the Nextcloud Login Flow v2 poll ───────────────────────────
    # Generated, not a hardcoded literal — keeps this a fake token, not a
    # credential pattern (SonarQube python:S2068).
    fake_app_password = secrets.token_urlsafe(24)
    completed = LoginFlowPollResult(
        status="completed",
        server="https://cloud.example.com",
        login_name="alice",
        app_password=fake_app_password,
    )
    flow_client = AsyncMock()
    flow_client.poll.return_value = completed

    provision_settings = MagicMock()
    provision_settings.nextcloud_host = "https://cloud.example.com"
    provision_settings.nextcloud_public_issuer_url = None

    provision_id = "login-flow-wake"
    _provision_sessions[provision_id] = {
        "status": "pending",
        "poll_endpoint": "https://cloud.example.com/login/v2/poll",
        "poll_token": "secret-token",
        "user_id": "alice",
        "created_at": time.time(),
        "expires_at": time.time() + 1200,
    }

    try:
        shutdown_event = anyio.Event()
        user_states: dict = {}

        async with anyio.create_task_group() as tg:
            await tg.start(
                user_manager_task,
                None,  # send_stream — unused by the stubbed scanner
                shutdown_event,
                anyio.Event(),  # scanner wake_event
                temp_storage,
                "https://cloud.example.com",
                user_states,
                tg,
                provision_signal,
            )

            # First poll: no users provisioned yet → no scanner.
            await anyio.sleep(0.1)
            assert not spawned

            # Run the real login-flow web-provision background task. It stores
            # the app password into temp_storage and rings the doorbell.
            with (
                patch(
                    "nextcloud_mcp_server.auth.provision_routes.get_settings",
                    return_value=provision_settings,
                ),
                patch(
                    "nextcloud_mcp_server.auth.provision_routes.get_nextcloud_ssl_verify",
                    return_value=False,
                ),
                patch(
                    "nextcloud_mcp_server.auth.provision_routes.LoginFlowV2Client",
                    return_value=flow_client,
                ),
                patch(
                    "nextcloud_mcp_server.auth.provision_routes.get_shared_storage",
                    new_callable=AsyncMock,
                    return_value=temp_storage,
                ),
            ):
                await _poll_and_store(provision_id)

            # The app password was really stored …
            assert "alice" in await temp_storage.get_all_app_password_user_ids()
            # … and the manager woke and spawned alice's scanner far inside the
            # 1000s poll interval (i.e. because of the signal, not the poll).
            with anyio.fail_after(3):
                await alice_spawned.wait()
            assert spawned == {"alice"}

            shutdown_event.set()
    finally:
        _provision_sessions.pop(provision_id, None)
