"""Unit tests for Login Flow v2 HTTP client.

Tests the LoginFlowV2Client with mocked HTTP responses for:
- Flow initiation (POST /index.php/login/v2)
- Flow polling (completed, pending, expired)
- Error handling
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextcloud_mcp_server.auth.login_flow import (
    LoginFlowInitResponse,
    LoginFlowPollResult,
    LoginFlowV2Client,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def flow_client():
    """Create a LoginFlowV2Client for testing."""
    return LoginFlowV2Client(
        nextcloud_host="https://cloud.example.com",
        verify_ssl=False,
    )


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    """Create a mock httpx response."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError

        response.raise_for_status.side_effect = HTTPStatusError(
            "error", request=MagicMock(), response=response
        )
    return response


async def test_initiate_success(flow_client):
    """Test successful Login Flow v2 initiation."""
    mock_response = _mock_response(
        200,
        {
            "login": "https://cloud.example.com/login/v2/grant?token=abc123",
            "poll": {
                "endpoint": "https://cloud.example.com/login/v2/poll",
                "token": "secret-poll-token",
            },
        },
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "nextcloud_mcp_server.auth.login_flow.nextcloud_httpx_client",
        return_value=mock_client,
    ):
        result = await flow_client.initiate()

    assert isinstance(result, LoginFlowInitResponse)
    assert result.login_url == "https://cloud.example.com/login/v2/grant?token=abc123"
    assert result.poll_endpoint == "https://cloud.example.com/login/v2/poll"
    assert result.poll_token == "secret-poll-token"


async def test_poll_completed(flow_client):
    """Test polling when user has completed login."""
    mock_response = _mock_response(
        200,
        {
            "server": "https://cloud.example.com",
            "loginName": "alice",
            "appPassword": "aaaaa-bbbbb-ccccc-ddddd-eeeee",
        },
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "nextcloud_mcp_server.auth.login_flow.nextcloud_httpx_client",
        return_value=mock_client,
    ):
        result = await flow_client.poll(
            poll_endpoint="https://cloud.example.com/login/v2/poll",
            poll_token="secret-poll-token",
        )

    assert isinstance(result, LoginFlowPollResult)
    assert result.status == "completed"
    assert result.server == "https://cloud.example.com"
    assert result.login_name == "alice"
    assert result.app_password == "aaaaa-bbbbb-ccccc-ddddd-eeeee"


async def test_poll_pending(flow_client):
    """Test polling when login is still pending."""
    mock_response = _mock_response(404, {})

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "nextcloud_mcp_server.auth.login_flow.nextcloud_httpx_client",
        return_value=mock_client,
    ):
        result = await flow_client.poll(
            poll_endpoint="https://cloud.example.com/login/v2/poll",
            poll_token="secret-poll-token",
        )

    assert result.status == "pending"
    assert result.server is None
    assert result.app_password is None


async def test_poll_expired(flow_client):
    """Test polling when flow has expired."""
    mock_response = _mock_response(403, {})

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "nextcloud_mcp_server.auth.login_flow.nextcloud_httpx_client",
        return_value=mock_client,
    ):
        result = await flow_client.poll(
            poll_endpoint="https://cloud.example.com/login/v2/poll",
            poll_token="expired-token",
        )

    assert result.status == "expired"
    assert result.app_password is None


async def test_initiate_with_custom_user_agent(flow_client):
    """Test that custom user agent is passed in the request."""
    mock_response = _mock_response(
        200,
        {
            "login": "https://cloud.example.com/login/v2/grant?token=abc",
            "poll": {
                "endpoint": "https://cloud.example.com/login/v2/poll",
                "token": "tok",
            },
        },
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "nextcloud_mcp_server.auth.login_flow.nextcloud_httpx_client",
        return_value=mock_client,
    ):
        await flow_client.initiate(user_agent="my-custom-agent")

    # Verify the user agent was passed
    call_kwargs = mock_client.post.call_args
    assert call_kwargs.kwargs["headers"]["User-Agent"] == "my-custom-agent"


async def test_login_flow_init_response_model():
    """Test LoginFlowInitResponse Pydantic model validation."""
    resp = LoginFlowInitResponse(
        login_url="https://cloud.example.com/login",
        poll_endpoint="https://cloud.example.com/poll",
        poll_token="token123",
    )
    assert resp.login_url == "https://cloud.example.com/login"
    assert resp.poll_endpoint == "https://cloud.example.com/poll"
    assert resp.poll_token == "token123"


async def test_login_flow_poll_result_model():
    """Test LoginFlowPollResult Pydantic model validation."""
    # Completed result
    completed = LoginFlowPollResult(
        status="completed",
        server="https://cloud.example.com",
        login_name="bob",
        app_password="xxxxx-yyyyy-zzzzz-aaaaa-bbbbb",
    )
    assert completed.status == "completed"
    assert completed.login_name == "bob"

    # Pending result
    pending = LoginFlowPollResult(status="pending")
    assert pending.status == "pending"
    assert pending.server is None
    assert pending.app_password is None
