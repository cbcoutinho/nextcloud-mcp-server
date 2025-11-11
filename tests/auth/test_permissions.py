"""Unit tests for permission checking."""

import pytest
from httpx import AsyncClient

from nextcloud_mcp_server.auth.permissions import is_nextcloud_admin
from nextcloud_mcp_server.client.users import UsersClient


@pytest.fixture
def mock_request(mocker):
    """Create a mock Starlette request."""
    request = mocker.Mock()
    request.user = mocker.Mock()
    request.user.display_name = "testuser"
    return request


@pytest.fixture
def mock_http_client(mocker):
    """Create a mock HTTP client."""
    return mocker.AsyncMock(spec=AsyncClient)


@pytest.mark.unit
async def test_is_nextcloud_admin_true(mock_request, mock_http_client, mocker):
    """Test checking if user is admin (admin group membership)."""
    # Mock the get_user_groups method to return admin group
    mock_get_user_groups = mocker.patch.object(
        UsersClient, "get_user_groups", return_value=["admin", "users"]
    )

    is_admin = await is_nextcloud_admin(mock_request, mock_http_client)

    assert is_admin is True
    mock_get_user_groups.assert_called_once_with("testuser")


@pytest.mark.unit
async def test_is_nextcloud_admin_false(mock_request, mock_http_client, mocker):
    """Test checking if user is not admin (no admin group membership)."""
    # Mock the get_user_groups method to return no admin group
    mock_get_user_groups = mocker.patch.object(
        UsersClient, "get_user_groups", return_value=["users", "editors"]
    )

    is_admin = await is_nextcloud_admin(mock_request, mock_http_client)

    assert is_admin is False
    mock_get_user_groups.assert_called_once_with("testuser")


@pytest.mark.unit
async def test_is_nextcloud_admin_empty_groups(mock_request, mock_http_client, mocker):
    """Test checking admin status when user has no groups."""
    # Mock the get_user_groups method to return empty list
    mock_get_user_groups = mocker.patch.object(
        UsersClient, "get_user_groups", return_value=[]
    )

    is_admin = await is_nextcloud_admin(mock_request, mock_http_client)

    assert is_admin is False
    mock_get_user_groups.assert_called_once_with("testuser")


@pytest.mark.unit
async def test_is_nextcloud_admin_no_username(mock_request, mock_http_client, mocker):
    """Test checking admin status when username is missing."""
    # Set username to None
    mock_request.user.display_name = None

    mock_get_user_groups = mocker.patch.object(UsersClient, "get_user_groups")

    is_admin = await is_nextcloud_admin(mock_request, mock_http_client)

    assert is_admin is False
    # Ensure get_user_groups was not called
    mock_get_user_groups.assert_not_called()


@pytest.mark.unit
async def test_is_nextcloud_admin_api_error(mock_request, mock_http_client, mocker):
    """Test checking admin status when API call fails."""
    # Mock the get_user_groups method to raise an exception
    mock_get_user_groups = mocker.patch.object(
        UsersClient,
        "get_user_groups",
        side_effect=Exception("API error"),
    )

    is_admin = await is_nextcloud_admin(mock_request, mock_http_client)

    assert is_admin is False
    mock_get_user_groups.assert_called_once_with("testuser")


@pytest.mark.unit
async def test_is_nextcloud_admin_case_sensitive(
    mock_request, mock_http_client, mocker
):
    """Test that admin group check is case-sensitive."""
    # Mock with "Admin" (capital A) instead of "admin"
    mock_get_user_groups = mocker.patch.object(
        UsersClient, "get_user_groups", return_value=["Admin", "users"]
    )

    is_admin = await is_nextcloud_admin(mock_request, mock_http_client)

    # Should be False because Nextcloud uses lowercase "admin"
    assert is_admin is False
    mock_get_user_groups.assert_called_once_with("testuser")
