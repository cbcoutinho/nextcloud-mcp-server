"""Unit tests for WebhooksClient."""

import pytest
from httpx import AsyncClient

from nextcloud_mcp_server.client.webhooks import WebhooksClient


@pytest.fixture
def webhooks_client(mocker):
    """Create a WebhooksClient with mocked HTTP client."""
    mock_http_client = mocker.AsyncMock(spec=AsyncClient)
    return WebhooksClient(mock_http_client, "testuser")


@pytest.mark.unit
async def test_list_webhooks(webhooks_client, mocker):
    """Test listing registered webhooks."""
    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "ocs": {
            "data": [
                {
                    "id": 1,
                    "uri": "http://example.com/webhook",
                    "event": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
                    "httpMethod": "POST",
                },
                {
                    "id": 2,
                    "uri": "http://example.com/webhook",
                    "event": "OCP\\Files\\Events\\Node\\NodeWrittenEvent",
                    "httpMethod": "POST",
                },
            ]
        }
    }

    mock_make_request = mocker.patch.object(
        WebhooksClient, "_make_request", return_value=mock_response
    )

    webhooks = await webhooks_client.list_webhooks()

    assert len(webhooks) == 2
    assert webhooks[0]["id"] == 1
    assert webhooks[0]["event"] == "OCP\\Files\\Events\\Node\\NodeCreatedEvent"
    assert webhooks[1]["id"] == 2

    mock_make_request.assert_called_once_with(
        "GET",
        "/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks",
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
    )


@pytest.mark.unit
async def test_list_webhooks_empty(webhooks_client, mocker):
    """Test listing webhooks when none are registered."""
    mock_response = mocker.Mock()
    mock_response.json.return_value = {"ocs": {"data": []}}

    mocker.patch.object(WebhooksClient, "_make_request", return_value=mock_response)

    webhooks = await webhooks_client.list_webhooks()

    assert webhooks == []


@pytest.mark.unit
async def test_create_webhook(webhooks_client, mocker):
    """Test creating a webhook registration."""
    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "ocs": {
            "data": {
                "id": 123,
                "uri": "http://example.com/webhook",
                "event": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
                "httpMethod": "POST",
                "authMethod": "none",
            }
        }
    }

    mock_make_request = mocker.patch.object(
        WebhooksClient, "_make_request", return_value=mock_response
    )

    webhook_data = await webhooks_client.create_webhook(
        event="OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        uri="http://example.com/webhook",
    )

    assert webhook_data["id"] == 123
    assert webhook_data["event"] == "OCP\\Files\\Events\\Node\\NodeCreatedEvent"

    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1] == "/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks"


@pytest.mark.unit
async def test_create_webhook_with_filter(webhooks_client, mocker):
    """Test creating a webhook with event filter."""
    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "ocs": {
            "data": {
                "id": 124,
                "uri": "http://example.com/webhook",
                "event": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
                "eventFilter": {"user.uid": "bob"},
            }
        }
    }

    mock_make_request = mocker.patch.object(
        WebhooksClient, "_make_request", return_value=mock_response
    )

    webhook_data = await webhooks_client.create_webhook(
        event="OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        uri="http://example.com/webhook",
        event_filter={"user.uid": "bob"},
    )

    assert webhook_data["id"] == 124
    assert webhook_data["eventFilter"] == {"user.uid": "bob"}

    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[1]["json"]["eventFilter"] == {"user.uid": "bob"}


@pytest.mark.unit
async def test_create_webhook_with_auth_headers(webhooks_client, mocker):
    """Test creating a webhook with authentication headers."""
    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "ocs": {
            "data": {
                "id": 125,
                "uri": "http://example.com/webhook",
                "event": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
                "authMethod": "bearer",
            }
        }
    }

    mock_make_request = mocker.patch.object(
        WebhooksClient, "_make_request", return_value=mock_response
    )

    webhook_data = await webhooks_client.create_webhook(
        event="OCP\\Files\\Events\\Node\\NodeCreatedEvent",
        uri="http://example.com/webhook",
        auth_method="bearer",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert webhook_data["id"] == 125
    assert webhook_data["authMethod"] == "bearer"

    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args
    assert call_args[1]["json"]["authMethod"] == "bearer"
    assert call_args[1]["json"]["headers"] == {"Authorization": "Bearer secret-token"}


@pytest.mark.unit
async def test_delete_webhook(webhooks_client, mocker):
    """Test deleting a webhook registration."""
    mock_response = mocker.Mock()

    mock_make_request = mocker.patch.object(
        WebhooksClient, "_make_request", return_value=mock_response
    )

    await webhooks_client.delete_webhook(webhook_id=123)

    mock_make_request.assert_called_once_with(
        "DELETE",
        "/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks/123",
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
    )


@pytest.mark.unit
async def test_get_webhook(webhooks_client, mocker):
    """Test getting a specific webhook by ID."""
    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "ocs": {
            "data": {
                "id": 123,
                "uri": "http://example.com/webhook",
                "event": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
                "httpMethod": "POST",
            }
        }
    }

    mock_make_request = mocker.patch.object(
        WebhooksClient, "_make_request", return_value=mock_response
    )

    webhook_data = await webhooks_client.get_webhook(webhook_id=123)

    assert webhook_data["id"] == 123
    assert webhook_data["event"] == "OCP\\Files\\Events\\Node\\NodeCreatedEvent"

    mock_make_request.assert_called_once_with(
        "GET",
        "/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks/123",
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
    )
