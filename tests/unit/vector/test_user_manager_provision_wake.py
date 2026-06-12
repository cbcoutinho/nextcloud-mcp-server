"""Unit tests for immediate-on-provision scanner spawning.

Covers the doorbell that lets a provisioning request wake ``user_manager_task``
at once instead of waiting out ``VECTOR_SYNC_USER_POLL_INTERVAL``:

- ``ProvisionSignal`` semantics (sticky ring, wake-a-parked-wait, re-arm).
- ``user_manager_task`` re-polls early when the signal is rung, spawning a
  newly provisioned user's scanner well before the next poll tick.
- ``notify_user_provisioned`` is a no-op when no manager is running.
"""

from unittest.mock import MagicMock

import anyio
import pytest

from nextcloud_mcp_server.vector.oauth_sync import ProvisionSignal, user_manager_task

pytestmark = pytest.mark.unit


# ── ProvisionSignal primitive ────────────────────────────────────────────────


async def test_provision_signal_ring_before_wait_is_observed():
    """A ring that lands before wait() is sticky and returns immediately."""
    signal = ProvisionSignal()
    signal.ring()
    with anyio.fail_after(1):
        await signal.wait()


async def test_provision_signal_wakes_parked_waiter():
    """ring() releases a wait() that is already parked."""
    signal = ProvisionSignal()
    woke = anyio.Event()

    async def waiter():
        await signal.wait()
        woke.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(waiter)
        await anyio.sleep(0.05)  # let waiter park
        assert not woke.is_set()
        signal.ring()
        with anyio.fail_after(1):
            await woke.wait()


async def test_provision_signal_rearms_for_next_cycle():
    """After a ring is consumed, the next wait() blocks until the next ring."""
    signal = ProvisionSignal()
    signal.ring()
    await signal.wait()  # consumes first ring, re-arms

    # Second wait must block (no pending ring) then release on the next ring.
    second = anyio.Event()

    async def waiter():
        await signal.wait()
        second.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(waiter)
        await anyio.sleep(0.05)
        assert not second.is_set()  # proves the first ring did not carry over
        signal.ring()
        with anyio.fail_after(1):
            await second.wait()


# ── user_manager_task wake-on-provision ──────────────────────────────────────


class _FakeStorage:
    """Storage stub whose provisioned-user set the test mutates between polls."""

    def __init__(self, users: set[str]):
        self.users = users

    async def get_all_app_password_user_ids(self) -> list[str]:
        return list(self.users)


async def test_user_manager_wakes_on_provision_signal(mocker):
    """Ringing the signal makes the manager re-poll and spawn the new user's
    scanner well before the (long) poll interval elapses."""
    # Long poll interval so any prompt spawn proves it was the signal, not poll.
    settings = MagicMock()
    settings.vector_sync_user_poll_interval = 1000
    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.get_settings", return_value=settings
    )

    spawned: set[str] = set()
    spawn_events: dict[str, anyio.Event] = {
        "alice": anyio.Event(),
        "bob": anyio.Event(),
    }

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
        if user_id in spawn_events:
            spawn_events[user_id].set()
        # Stay alive (keeps user_states populated) until shutdown.
        with cancel_scope:
            await shutdown_event.wait()
        user_states.pop(user_id, None)

    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync._run_user_scanner_with_scope",
        fake_scanner,
    )

    storage = _FakeStorage({"alice"})
    provision_signal = ProvisionSignal()
    shutdown_event = anyio.Event()
    scanner_wake_event = anyio.Event()
    user_states: dict = {}

    async with anyio.create_task_group() as tg:
        await tg.start(
            user_manager_task,
            None,  # send_stream — unused by the stubbed scanner
            shutdown_event,
            scanner_wake_event,
            storage,
            "https://nextcloud",
            user_states,
            tg,
            provision_signal,
        )

        # First poll discovers the already-provisioned user.
        with anyio.fail_after(2):
            await spawn_events["alice"].wait()
        assert "bob" not in spawned

        # Provision a new user, then ring — the manager must pick bob up fast.
        storage.users.add("bob")
        provision_signal.ring()
        with anyio.fail_after(2):  # << 1000s poll interval
            await spawn_events["bob"].wait()
        assert spawned == {"alice", "bob"}

        shutdown_event.set()


async def test_user_manager_shutdown_still_breaks_sleep(mocker):
    """Setting shutdown wakes the manager out of its sleep promptly even with a
    long poll interval (the doorbell race must not regress shutdown latency)."""
    settings = MagicMock()
    settings.vector_sync_user_poll_interval = 1000
    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync.get_settings", return_value=settings
    )

    async def _unused_scanner(*args, **kwargs):
        # No users provisioned, so this is never called; keep a harmless stub.
        return None

    mocker.patch(
        "nextcloud_mcp_server.vector.oauth_sync._run_user_scanner_with_scope",
        _unused_scanner,
    )

    storage = _FakeStorage(set())
    shutdown_event = anyio.Event()

    # fail_after wraps the whole task group: if shutdown_event doesn't break the
    # 1000s sleep, the task group never exits and the 2s deadline trips.
    with anyio.fail_after(2):
        async with anyio.create_task_group() as tg:
            await tg.start(
                user_manager_task,
                None,
                shutdown_event,
                anyio.Event(),
                storage,
                "https://nextcloud",
                {},
                tg,
                ProvisionSignal(),
            )
            await anyio.sleep(0.05)  # let it enter the sleep
            shutdown_event.set()


# ── notify_user_provisioned no-op guard ──────────────────────────────────────


def test_notify_user_provisioned_noop_without_manager(mocker):
    """When no manager is running, the helper must not raise."""
    import nextcloud_mcp_server.app as app_module

    mocker.patch.object(app_module._vector_sync_state, "provision_signal", None)
    # Should be a silent no-op.
    app_module.notify_user_provisioned()


async def test_notify_user_provisioned_rings_when_present(mocker):
    """When a manager is running, the helper rings its signal."""
    import nextcloud_mcp_server.app as app_module

    signal = ProvisionSignal()
    mocker.patch.object(app_module._vector_sync_state, "provision_signal", signal)
    app_module.notify_user_provisioned()
    # Public contract: after a ring, the next wait() returns without blocking.
    with anyio.fail_after(1):
        await signal.wait()
