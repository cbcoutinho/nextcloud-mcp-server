"""
Unit tests for the Management API /api/v1/apps endpoint.

The handler hits ``/ocs/v2.php/cloud/capabilities`` (not the legacy admin-only
``/ocs/v1.php/cloud/apps`` which was ``@PasswordConfirmationRequired``) and
authenticates via the user's stored Login Flow v2 app password using HTTP
Basic Auth. The OAuth bearer is **never** forwarded to Nextcloud (see
``docs/login-flow-v2.md`` and ADR-022).
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.apps import get_installed_apps
from nextcloud_mcp_server.auth.scope_authorization import ProvisioningRequiredError

pytestmark = pytest.mark.unit


def _build_test_app() -> Starlette:
    app = Starlette(routes=[Route("/api/v1/apps", get_installed_apps, methods=["GET"])])
    app.state.oauth_context = {"config": {"nextcloud_host": "http://nc.test"}}
    return app


def _patch_token_validation(mocker, user_id: str = "admin") -> None:
    mocker.patch(
        "nextcloud_mcp_server.api.apps.validate_token_and_get_user",
        new=AsyncMock(return_value=(user_id, {"sub": user_id})),
    )


def _patch_basic_auth(
    mocker, username: str = "admin", app_password: str = "stored-app-pwd"
) -> AsyncMock:
    """Patch get_basic_auth_for_user to return canned credentials."""
    return mocker.patch(
        "nextcloud_mcp_server.api.apps.get_basic_auth_for_user",
        new=AsyncMock(return_value=(username, app_password)),
    )


def _patch_outbound_client(mocker, response: MagicMock) -> MagicMock:
    """Patch the outbound httpx client; return the factory MagicMock so tests
    can introspect the kwargs (esp. ``auth=``) it was called with."""
    mock_get = AsyncMock(return_value=response)
    mock_client = AsyncMock()
    mock_client.get = mock_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    # Must return False (or None) so async-with does NOT suppress exceptions
    # raised inside the block — default AsyncMock() resolves to a truthy
    # MagicMock and would silently swallow ValueError.
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_client)
    mocker.patch("nextcloud_mcp_server.api.apps.nextcloud_httpx_client", mock_factory)
    mock_factory.attach_mock(mock_get, "get")
    return mock_factory


def _capabilities_response(capabilities: dict[str, dict]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"ocs": {"data": {"capabilities": capabilities}}}
    return response


async def test_returns_sorted_capability_keys(mocker):
    """Happy path: handler hits OCS v2 capabilities and returns sorted app keys."""
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker)
    response = _capabilities_response(
        {
            "notes": {"api_version": "1.4"},
            "files": {"bigfilechunking": True},
            "core": {"webdav-root": "remote.php/webdav"},
            "tables": {"foo": "bar"},
        }
    )
    factory = _patch_outbound_client(mocker, response)

    app = _build_test_app()
    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 200
    assert http_response.json() == {"apps": ["core", "files", "notes", "tables"]}

    # Outbound URL is OCS v2 capabilities, NOT v1 cloud/apps.
    factory.get.assert_awaited_once()
    called_path = factory.get.call_args.args[0]
    assert called_path == "/ocs/v2.php/cloud/capabilities"

    # Outbound auth is BasicAuth (NOT Bearer) — the OAuth token must not be
    # forwarded to Nextcloud per ADR-022 / docs/login-flow-v2.md.
    factory_kwargs = factory.call_args.kwargs
    assert "headers" not in factory_kwargs or "Authorization" not in (
        factory_kwargs.get("headers") or {}
    )
    assert isinstance(factory_kwargs["auth"], httpx.BasicAuth)


async def test_empty_capabilities_returns_empty_list(mocker):
    """Empty capabilities map → empty apps list, not an error."""
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker)
    response = _capabilities_response({})
    _patch_outbound_client(mocker, response)

    app = _build_test_app()
    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 200
    assert http_response.json() == {"apps": []}


async def test_ocs_error_returns_500_with_sanitized_message(mocker):
    """Non-200 from Nextcloud OCS surfaces as a generic 500 to the caller."""
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker)
    error_response = MagicMock()
    error_response.status_code = 503
    _patch_outbound_client(mocker, error_response)

    app = _build_test_app()
    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 500
    body = http_response.json()
    assert body["error"] == "Internal error"
    # _sanitize_error_for_client returns a generic message; specific status
    # code must NOT leak to the client
    assert "503" not in body["message"]


async def test_missing_nextcloud_host_returns_500(mocker):
    """Misconfigured oauth_context (no nextcloud_host) surfaces as 500."""
    _patch_token_validation(mocker)
    _patch_basic_auth(mocker)
    response = _capabilities_response({})
    _patch_outbound_client(mocker, response)

    app = Starlette(routes=[Route("/api/v1/apps", get_installed_apps, methods=["GET"])])
    app.state.oauth_context = {"config": {}}  # no nextcloud_host

    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 500


async def test_unprovisioned_user_returns_428(mocker):
    """Users without a stored app password get HTTP 428 (Precondition Required,
    RFC 6585) so the client can surface a 'complete Login Flow v2' UX rather
    than a generic 500. 428 is the right semantic — the request requires a
    prerequisite step (provisioning) before it can succeed."""
    _patch_token_validation(mocker)
    mocker.patch(
        "nextcloud_mcp_server.api.apps.get_basic_auth_for_user",
        new=AsyncMock(side_effect=ProvisioningRequiredError("not provisioned")),
    )

    app = _build_test_app()
    client = TestClient(app)
    http_response = client.get(
        "/api/v1/apps", headers={"Authorization": "Bearer test-token"}
    )

    assert http_response.status_code == 428
    assert http_response.json()["error"] == "Provisioning required"
