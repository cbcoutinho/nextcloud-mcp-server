"""Unit tests for ``_register_preset_webhooks``.

The helper threads ``webhook_auth_pair()`` into each ``create_webhook``
call, so it is the integration point between the secret-resolution logic
and the OCS client. These tests verify the wiring without standing up a
full Starlette app.
"""

import pytest

from nextcloud_mcp_server.auth import webhook_routes
from nextcloud_mcp_server.auth.webhook_routes import (
    WebhookSecretNotConfigured,
    _register_preset_webhooks,
)
from nextcloud_mcp_server.client.webhooks import WebhooksClient
from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.server.webhook_presets import get_preset

pytestmark = pytest.mark.unit


def _patch_secret(monkeypatch, secret: str | None) -> None:
    monkeypatch.setattr(
        webhook_routes,
        "get_settings",
        lambda: Settings(webhook_secret=secret),
    )


def _make_webhooks_client(mocker, ids: list[int]):
    """Mock WebhooksClient.create_webhook to return one fake webhook per id."""
    client = mocker.AsyncMock(spec=WebhooksClient)
    client.create_webhook.side_effect = [{"id": i} for i in ids]
    return client


async def test_register_threads_bearer_auth_when_secret_set(monkeypatch, mocker):
    _patch_secret(monkeypatch, "supersecret")
    preset = get_preset("notes_sync")
    assert preset is not None
    client = _make_webhooks_client(mocker, ids=[101, 102, 103])

    registered = await _register_preset_webhooks(
        client, preset, "https://mcp.example.com/webhooks/nextcloud"
    )

    assert registered == [101, 102, 103]
    assert client.create_webhook.await_count == len(preset["events"])

    expected_auth = {"Authorization": "Bearer supersecret"}
    for call, event_config in zip(
        client.create_webhook.await_args_list, preset["events"]
    ):
        kwargs = call.kwargs
        assert kwargs["event"] == event_config["event"]
        assert kwargs["uri"] == "https://mcp.example.com/webhooks/nextcloud"
        assert kwargs["auth_method"] == "header"
        assert kwargs["auth_data"] == expected_auth
        # notes_sync uses path filters; ensure they round-trip through the helper
        assert kwargs["event_filter"] == event_config["filter"]


async def test_register_refuses_when_secret_unset(monkeypatch, mocker):
    """Security (GHSA-8vh3-g2qg-2h2c): webhooks require WEBHOOK_SECRET.
    Without it, registration raises instead of creating a dead, unauthenticated
    (``authMethod="none"``) delivery target pointing at a disabled receiver."""
    _patch_secret(monkeypatch, None)
    preset = get_preset("notes_sync")
    assert preset is not None
    client = _make_webhooks_client(mocker, ids=[1, 2, 3])

    with pytest.raises(WebhookSecretNotConfigured):
        await _register_preset_webhooks(
            client, preset, "https://mcp.example.com/webhooks/nextcloud"
        )

    client.create_webhook.assert_not_called()


async def test_register_returns_ids_in_call_order(monkeypatch, mocker):
    _patch_secret(monkeypatch, "supersecret")
    preset = get_preset("notes_sync")
    assert preset is not None
    client = _make_webhooks_client(mocker, ids=[42, 43, 44])

    ids = await _register_preset_webhooks(client, preset, "https://example.com/wh")

    assert ids == [42, 43, 44]


async def test_register_rolls_back_already_created_webhooks_on_failure(
    monkeypatch, mocker
):
    """A mid-loop failure must not leave orphaned webhooks live in Nextcloud.

    ``_register_preset_webhooks`` only returns after every event registers, so
    the caller's ``store_webhook`` never runs when event N fails — previously
    leaving events 1..N-1 live in Nextcloud but absent from the DB, invisible to
    the UI and still delivering, while the handler reported "Failed to enable
    preset" (i.e. that nothing happened).
    """
    _patch_secret(monkeypatch, "supersecret")
    preset = get_preset("notes_sync")
    assert preset is not None

    client = mocker.AsyncMock(spec=WebhooksClient)
    # Events 1 and 2 succeed, event 3 fails.
    client.create_webhook.side_effect = [
        {"id": 42},
        {"id": 43},
        RuntimeError("nextcloud exploded"),
    ]

    with pytest.raises(RuntimeError, match="nextcloud exploded"):
        await _register_preset_webhooks(client, preset, "https://example.com/wh")

    # The two that were created must be deleted again, so the reported end state
    # ("nothing enabled") matches reality.
    assert [c.args[0] for c in client.delete_webhook.call_args_list] == [42, 43]


async def test_register_rollback_failure_does_not_mask_original_error(
    monkeypatch, mocker
):
    """A failed rollback must not replace the error that explains the failure."""
    _patch_secret(monkeypatch, "supersecret")
    preset = get_preset("notes_sync")
    assert preset is not None

    client = mocker.AsyncMock(spec=WebhooksClient)
    client.create_webhook.side_effect = [{"id": 42}, RuntimeError("original cause")]
    client.delete_webhook.side_effect = RuntimeError("cleanup also failed")

    # The original error propagates, not the cleanup error.
    with pytest.raises(RuntimeError, match="original cause"):
        await _register_preset_webhooks(client, preset, "https://example.com/wh")


async def test_enabled_presets_failure_propagates_rather_than_reporting_none_enabled(
    mocker,
):
    """A listing failure must not masquerade as "no presets are enabled".

    Returning {} made the pane render "Not Enabled" for live presets, so an admin
    clicking Enable would double-register every event.
    """
    client = mocker.AsyncMock(spec=WebhooksClient)
    client.list_webhooks.side_effect = RuntimeError("nextcloud unreachable")

    with pytest.raises(RuntimeError, match="nextcloud unreachable"):
        await webhook_routes._get_enabled_presets(client)
