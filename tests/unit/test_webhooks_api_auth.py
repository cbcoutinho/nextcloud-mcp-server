"""Unit tests asserting the webhook API endpoints authenticate to Nextcloud
with an app-password BasicAuth credential — never by forwarding the inbound
OAuth bearer token.

Per ADR-022 / ``docs/login-flow-v2.md`` the data leg from MCP server to
Nextcloud always uses HTTP Basic Auth with a per-user app password obtained
via Login Flow v2. Forwarding OAuth bearers to Nextcloud was the obsolete
pre-ADR-022 pattern; it relies on upstream user_oidc patches that were never
merged and is incompatible with admin endpoints gated by
``@PasswordConfirmationRequired`` (e.g. ``webhook_listeners``).
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.webhooks import (
    create_webhook,
    delete_webhook,
    list_webhooks,
)
from nextcloud_mcp_server.auth.scope_authorization import ProvisioningRequiredError
from nextcloud_mcp_server.auth.webhook_routes import WebhookSecretNotConfigured

pytestmark = pytest.mark.unit


def _build_test_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/v1/webhooks", list_webhooks, methods=["GET"]),
            Route("/api/v1/webhooks", create_webhook, methods=["POST"]),
            Route("/api/v1/webhooks/{webhook_id}", delete_webhook, methods=["DELETE"]),
        ]
    )
    app.state.oauth_context = {"config": {"nextcloud_host": "http://nc.test"}}
    return app


def _patch_token_validation(mocker, user_id: str = "admin") -> None:
    mocker.patch(
        "nextcloud_mcp_server.api.webhooks.validate_token_and_get_user",
        new=AsyncMock(return_value=(user_id, {"sub": user_id})),
    )


def _patch_basic_auth(
    mocker, username: str = "admin", app_password: str = "stored-app-pwd"
) -> AsyncMock:
    return mocker.patch(
        "nextcloud_mcp_server.api.webhooks.get_basic_auth_for_user",
        new=AsyncMock(return_value=(username, app_password)),
    )


def _patch_webhooks_client(mocker, **methods) -> MagicMock:
    """Replace WebhooksClient with a stub. ``methods`` maps method name →
    return value for AsyncMock(side_effect/return_value)."""
    instance = MagicMock()
    for name, value in methods.items():
        setattr(instance, name, AsyncMock(return_value=value))

    cls = MagicMock(return_value=instance)
    mocker.patch("nextcloud_mcp_server.api.webhooks.WebhooksClient", cls)
    return cls


def _patch_outbound_client_factory(mocker) -> MagicMock:
    """Patch nextcloud_httpx_client so we can assert on the kwargs (esp.
    ``auth=``) the handler called it with."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=mock_client)
    mocker.patch("nextcloud_mcp_server.api.webhooks.nextcloud_httpx_client", factory)
    return factory


def _assert_basic_auth_not_bearer(factory: MagicMock) -> None:
    """Outbound httpx kwargs must use ``auth=BasicAuth(...)`` and NOT a
    Bearer ``Authorization`` header."""
    factory.assert_called_once()
    kwargs = factory.call_args.kwargs
    assert isinstance(kwargs["auth"], httpx.BasicAuth), (
        "Outbound NC request must use BasicAuth, not bearer-header forwarding"
    )
    headers = kwargs.get("headers") or {}
    assert "Authorization" not in headers, (
        "Outbound NC request must NOT carry an Authorization header — "
        "the OAuth bearer must never be forwarded to Nextcloud"
    )


# ---------------------------------------------------------------------------
# list_webhooks
# ---------------------------------------------------------------------------


async def test_list_webhooks_uses_basic_auth(mocker):
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker, username="alice", app_password="alice-pwd")
    factory = _patch_outbound_client_factory(mocker)
    _patch_webhooks_client(
        mocker, list_webhooks=[{"id": 1, "event": "test", "uri": "http://x"}]
    )

    client = TestClient(_build_test_app())
    resp = client.get("/api/v1/webhooks", headers={"Authorization": "Bearer mcp-token"})

    assert resp.status_code == 200
    assert resp.json() == {"webhooks": [{"id": 1, "event": "test", "uri": "http://x"}]}
    _assert_basic_auth_not_bearer(factory)


async def test_list_webhooks_returns_428_when_unprovisioned(mocker):
    _patch_token_validation(mocker)
    mocker.patch(
        "nextcloud_mcp_server.api.webhooks.get_basic_auth_for_user",
        new=AsyncMock(side_effect=ProvisioningRequiredError("not provisioned")),
    )

    client = TestClient(_build_test_app())
    resp = client.get("/api/v1/webhooks", headers={"Authorization": "Bearer mcp-token"})

    assert resp.status_code == 428
    assert resp.json()["error"] == "Provisioning required"


# ---------------------------------------------------------------------------
# create_webhook
# ---------------------------------------------------------------------------


async def test_create_webhook_uses_basic_auth(mocker):
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker, username="bob", app_password="bob-pwd")
    factory = _patch_outbound_client_factory(mocker)
    _patch_webhooks_client(
        mocker,
        create_webhook={"id": 42, "event": "OCP\\Events\\NodeCreated"},
    )
    mocker.patch(
        "nextcloud_mcp_server.api.webhooks.webhook_auth_pair",
        return_value=("header", {"Authorization": "Bearer supersecret"}),
    )

    client = TestClient(_build_test_app())
    resp = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": "Bearer mcp-token"},
        json={
            "event": "OCP\\Events\\NodeCreated",
            "uri": "http://mcp:8000/webhooks/nextcloud",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"webhook": {"id": 42, "event": "OCP\\Events\\NodeCreated"}}
    _assert_basic_auth_not_bearer(factory)


async def test_create_webhook_returns_503_when_secret_unset(mocker):
    """Security (GHSA-8vh3-g2qg-2h2c): registration is refused without a
    WEBHOOK_SECRET so no unauthenticated delivery target is created."""
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker, username="bob", app_password="bob-pwd")
    _patch_outbound_client_factory(mocker)
    mocker.patch(
        "nextcloud_mcp_server.api.webhooks.webhook_auth_pair",
        side_effect=WebhookSecretNotConfigured("WEBHOOK_SECRET must be set"),
    )

    client = TestClient(_build_test_app())
    # https example URL — registration is refused before the uri is used, and
    # an https literal avoids a spurious S5332 "use https" hotspot in new code.
    resp = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": "Bearer mcp-token"},
        json={
            "event": "OCP\\Events\\NodeCreated",
            "uri": "https://mcp.example.com/webhooks/nextcloud",
        },
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == "Webhooks disabled"


async def test_create_webhook_validates_required_fields(mocker):
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker)

    client = TestClient(_build_test_app())
    resp = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": "Bearer mcp-token"},
        json={"event": "X"},  # missing uri
    )

    assert resp.status_code == 400


async def test_create_webhook_returns_428_when_unprovisioned(mocker):
    _patch_token_validation(mocker)
    mocker.patch(
        "nextcloud_mcp_server.api.webhooks.get_basic_auth_for_user",
        new=AsyncMock(side_effect=ProvisioningRequiredError("not provisioned")),
    )

    client = TestClient(_build_test_app())
    resp = client.post(
        "/api/v1/webhooks",
        headers={"Authorization": "Bearer mcp-token"},
        json={"event": "X", "uri": "http://x"},
    )

    assert resp.status_code == 428
    assert resp.json()["error"] == "Provisioning required"


# ---------------------------------------------------------------------------
# delete_webhook
# ---------------------------------------------------------------------------


async def test_delete_webhook_uses_basic_auth(mocker):
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker, username="carol", app_password="carol-pwd")
    factory = _patch_outbound_client_factory(mocker)
    _patch_webhooks_client(mocker, delete_webhook=None)

    client = TestClient(_build_test_app())
    resp = client.delete(
        "/api/v1/webhooks/99", headers={"Authorization": "Bearer mcp-token"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "message": "Webhook deleted"}
    _assert_basic_auth_not_bearer(factory)


async def test_delete_webhook_rejects_non_integer_id(mocker):
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker)

    client = TestClient(_build_test_app())
    resp = client.delete(
        "/api/v1/webhooks/notanumber",
        headers={"Authorization": "Bearer mcp-token"},
    )

    assert resp.status_code == 400


async def test_delete_webhook_returns_428_when_unprovisioned(mocker):
    _patch_token_validation(mocker)
    mocker.patch(
        "nextcloud_mcp_server.api.webhooks.get_basic_auth_for_user",
        new=AsyncMock(side_effect=ProvisioningRequiredError("not provisioned")),
    )

    client = TestClient(_build_test_app())
    resp = client.delete(
        "/api/v1/webhooks/99",
        headers={"Authorization": "Bearer mcp-token"},
    )

    assert resp.status_code == 428
    assert resp.json()["error"] == "Provisioning required"


# ---------------------------------------------------------------------------
# get_basic_auth_for_user helper
# ---------------------------------------------------------------------------


async def test_get_basic_auth_for_user_resolves_username_and_password(mocker):
    """Helper returns the stored Nextcloud username (not the OAuth user_id)
    when one was recorded at provisioning time."""
    from nextcloud_mcp_server.api._auth import get_basic_auth_for_user

    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(
        return_value={
            "app_password": "encrypted-then-decrypted-pwd",
            "scopes": ["notes.read"],
            "username": "nc-username",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-02",
        }
    )
    mocker.patch(
        "nextcloud_mcp_server.api._auth.get_shared_storage",
        new=AsyncMock(return_value=storage),
    )

    username, password = await get_basic_auth_for_user("idp-user-id")
    assert username == "nc-username"
    assert password == "encrypted-then-decrypted-pwd"


async def test_get_basic_auth_for_user_falls_back_to_user_id(mocker):
    """When ``username`` is null in storage, the helper falls back to the
    OAuth-issued user_id."""
    from nextcloud_mcp_server.api._auth import get_basic_auth_for_user

    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(
        return_value={
            "app_password": "pwd",
            "scopes": None,
            "username": None,
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }
    )
    mocker.patch(
        "nextcloud_mcp_server.api._auth.get_shared_storage",
        new=AsyncMock(return_value=storage),
    )

    username, _ = await get_basic_auth_for_user("admin")
    assert username == "admin"


async def test_get_basic_auth_for_user_raises_when_unprovisioned(mocker):
    from nextcloud_mcp_server.api._auth import get_basic_auth_for_user

    storage = MagicMock()
    storage.get_app_password_with_scopes = AsyncMock(return_value=None)
    mocker.patch(
        "nextcloud_mcp_server.api._auth.get_shared_storage",
        new=AsyncMock(return_value=storage),
    )

    with pytest.raises(ProvisioningRequiredError):
        await get_basic_auth_for_user("admin")


class TestCreateWebhookMalformedBody:
    """A malformed request body is a caller fault (400), not a server fault (500).

    ``await request.json()`` used to sit inside the handler's catch-all, so a bad
    payload surfaced as 500 "Internal error" — telling the client the server is
    broken when the client sent bad JSON, and burying a routine 400 in the error
    logs. ``delete_webhook`` already guarded its ``int()`` parse this way.
    """

    def test_malformed_json_returns_400_not_500(self, mocker):
        _patch_token_validation(mocker)
        client = TestClient(_build_test_app())

        response = client.post(
            "/api/v1/webhooks",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "Bad request"

    def test_non_object_json_body_returns_400_not_500(self, mocker):
        """A JSON array parses fine but has no .get() — previously an AttributeError
        inside the catch-all, i.e. another 500."""
        _patch_token_validation(mocker)
        client = TestClient(_build_test_app())

        response = client.post("/api/v1/webhooks", json=[])

        assert response.status_code == 400
        assert response.json()["error"] == "Bad request"

    def test_missing_fields_still_returns_400(self, mocker):
        """The pre-existing required-field check must survive the refactor."""
        _patch_token_validation(mocker)
        client = TestClient(_build_test_app())

        response = client.post("/api/v1/webhooks", json={"event": "SomeEvent"})

        assert response.status_code == 400
        assert "Missing required fields" in response.json()["message"]
