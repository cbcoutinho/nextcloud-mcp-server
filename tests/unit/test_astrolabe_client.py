"""Unit tests for ``AstrolabeClient.get_background_sync_status`` (ADR-029).

The Pact consumer contract (``tests/contract/``) pins the wire shape; these
mocked tests pin the *field mapping* that the original silent bug got wrong —
it read a non-existent ``app_password`` field, so ``has_access`` was always
``False``. A regression here now fails fast at the unit layer.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.auth.astrolabe_client import AstrolabeClient

pytestmark = pytest.mark.unit


def _patch_outbound_client(mocker, response: MagicMock) -> MagicMock:
    """Patch the httpx client used by AstrolabeClient; return the mock client."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mocker.patch(
        "nextcloud_mcp_server.auth.astrolabe_client.nextcloud_httpx_client",
        MagicMock(return_value=mock_client),
    )
    return mock_client


def _client(mocker) -> AstrolabeClient:
    client = AstrolabeClient(
        nextcloud_host="https://cloud.example.com",
        client_id="mcp",
        client_secret="secret",
    )
    mocker.patch.object(client, "get_access_token", AsyncMock(return_value="tok"))
    return client


async def test_provisioned_user_maps_status_fields(mocker):
    """200 + has_background_access=True maps to has_access/credential_type/provisioned_at."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "user_id": "alice",
        "has_background_access": True,
        "sync_type": "app_password",
        "provisioned_at": 1717000000,
    }
    mock_client = _patch_outbound_client(mocker, response)
    client = _client(mocker)

    status = await client.get_background_sync_status("alice")

    assert status == {
        "has_access": True,
        "credential_type": "app_password",
        "provisioned_at": 1717000000,
    }
    response.raise_for_status.assert_called_once()
    # The bearer token from get_access_token is forwarded on the request.
    _, kwargs = mock_client.get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


async def test_provisioned_false_reports_no_access(mocker):
    """200 + has_background_access=False reports no access (the bug's regression guard)."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "user_id": "bob",
        "has_background_access": False,
        "sync_type": None,
        "provisioned_at": None,
    }
    _patch_outbound_client(mocker, response)
    client = _client(mocker)

    status = await client.get_background_sync_status("bob")

    assert status["has_access"] is False
    assert status["credential_type"] is None
    assert status["provisioned_at"] is None


async def test_missing_credentials_404_returns_no_access(mocker):
    """404 short-circuits to no-access without touching raise_for_status/json."""
    response = MagicMock()
    response.status_code = 404
    _patch_outbound_client(mocker, response)
    client = _client(mocker)

    status = await client.get_background_sync_status("carol")

    assert status == {
        "has_access": False,
        "credential_type": None,
        "provisioned_at": None,
    }
    response.raise_for_status.assert_not_called()
    response.json.assert_not_called()
