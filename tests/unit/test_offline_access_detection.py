"""Tests for discovery-driven offline_access scope handling.

Verifies that the server conditionally includes ``offline_access`` in OAuth
scope requests based on the IdP's ``scopes_supported`` discovery field, and
that refresh tokens are always accepted from responses regardless of whether
``offline_access`` was requested (AWS Cognito behavior).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_storage():
    """Mock RefreshTokenStorage."""
    storage = AsyncMock()
    storage.get_refresh_token = AsyncMock(return_value=None)
    storage.store_refresh_token = AsyncMock()
    storage.delete_refresh_token = AsyncMock()
    return storage


def _make_broker(mock_storage):
    """Create a TokenBrokerService instance for testing."""
    return TokenBrokerService(
        storage=mock_storage,
        oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
        nextcloud_host="https://nextcloud.example.com",
        client_id="test_client_id",
        client_secret="test_client_secret",
        cache_ttl=300,
    )


class TestIdpSupportsOfflineAccess:
    """Test _idp_supports_offline_access() discovery-driven detection."""

    async def test_supports_when_listed(self, mock_storage):
        """Returns True when scopes_supported includes offline_access."""
        broker = _make_broker(mock_storage)
        discovery = {
            "token_endpoint": "https://idp.example.com/token",
            "scopes_supported": ["openid", "profile", "email", "offline_access"],
        }
        with patch.object(broker, "_get_oidc_config", return_value=discovery):
            assert await broker._idp_supports_offline_access() is True
        await broker.close()

    async def test_not_supported_when_absent_from_list(self, mock_storage):
        """Returns False when scopes_supported is present but lacks offline_access (Cognito)."""
        broker = _make_broker(mock_storage)
        discovery = {
            "token_endpoint": "https://idp.example.com/token",
            "scopes_supported": ["openid"],
        }
        with patch.object(broker, "_get_oidc_config", return_value=discovery):
            assert await broker._idp_supports_offline_access() is False
        await broker.close()

    async def test_supports_when_field_missing(self, mock_storage):
        """Returns True (safe default) when scopes_supported is absent from discovery."""
        broker = _make_broker(mock_storage)
        discovery = {
            "token_endpoint": "https://idp.example.com/token",
            # No scopes_supported field at all
        }
        with patch.object(broker, "_get_oidc_config", return_value=discovery):
            assert await broker._idp_supports_offline_access() is True
        await broker.close()


class TestRefreshScopeConditional:
    """Test that refresh methods conditionally include offline_access."""

    async def test_refresh_omits_offline_access_for_cognito(self, mock_storage):
        """When IdP does not support offline_access, scope string omits it."""
        broker = _make_broker(mock_storage)
        discovery = {
            "token_endpoint": "https://idp.example.com/token",
            "scopes_supported": ["openid", "profile", "email"],
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "expires_in": 3600,
        }

        with patch.object(broker, "_get_oidc_config", return_value=discovery):
            with patch.object(broker, "_get_http_client") as mock_client:
                mock_post = AsyncMock(return_value=mock_response)
                mock_client.return_value.post = mock_post

                await broker._refresh_access_token_with_scopes(
                    "test_refresh", ["notes.read"], user_id=None
                )

                # Verify the scope sent in the POST
                call_kwargs = mock_post.call_args
                posted_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get(
                    "data"
                )
                scope_str = posted_data["scope"]
                assert "offline_access" not in scope_str
                assert "openid" in scope_str
                assert "notes.read" in scope_str
        await broker.close()

    async def test_refresh_includes_offline_access_for_nextcloud(self, mock_storage):
        """When IdP supports offline_access, scope string includes it."""
        broker = _make_broker(mock_storage)
        discovery = {
            "token_endpoint": "https://idp.example.com/token",
            "scopes_supported": ["openid", "profile", "email", "offline_access"],
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "expires_in": 3600,
        }

        with patch.object(broker, "_get_oidc_config", return_value=discovery):
            with patch.object(broker, "_get_http_client") as mock_client:
                mock_post = AsyncMock(return_value=mock_response)
                mock_client.return_value.post = mock_post

                await broker._refresh_access_token_with_scopes(
                    "test_refresh", ["notes.read"], user_id=None
                )

                call_kwargs = mock_post.call_args
                posted_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get(
                    "data"
                )
                scope_str = posted_data["scope"]
                assert "offline_access" in scope_str
                assert "openid" in scope_str
        await broker.close()

    async def test_refresh_token_stored_without_offline_access_scope(
        self, mock_storage
    ):
        """Refresh token from response is stored even when offline_access wasn't requested.

        This is the key Cognito scenario: the IdP returns a refresh token
        automatically even though offline_access was not in the scope request.
        """
        broker = _make_broker(mock_storage)
        # Cognito-like discovery: no offline_access in scopes_supported
        discovery = {
            "token_endpoint": "https://idp.example.com/token",
            "scopes_supported": ["openid", "profile", "email"],
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "expires_in": 3600,
            # Cognito returns refresh token automatically
            "refresh_token": "rotated_refresh_token",
        }

        with patch.object(broker, "_get_oidc_config", return_value=discovery):
            with patch.object(broker, "_get_http_client") as mock_client:
                mock_client.return_value.post = AsyncMock(return_value=mock_response)

                await broker._refresh_access_token_with_scopes(
                    "old_refresh_token", ["notes.read"], user_id="user1"
                )

                # Verify the rotated refresh token was stored
                mock_storage.store_refresh_token.assert_called_once()
                call_kwargs = mock_storage.store_refresh_token.call_args[1]
                assert call_kwargs["user_id"] == "user1"
                assert call_kwargs["refresh_token"] == "rotated_refresh_token"
        await broker.close()

    async def test_deprecated_refresh_omits_offline_access_for_cognito(
        self, mock_storage
    ):
        """The deprecated _refresh_access_token also respects IdP discovery."""
        broker = _make_broker(mock_storage)
        discovery = {
            "token_endpoint": "https://idp.example.com/token",
            "scopes_supported": ["openid", "profile"],
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "expires_in": 3600,
        }

        with patch.object(broker, "_get_oidc_config", return_value=discovery):
            with patch.object(broker, "_get_http_client") as mock_client:
                mock_post = AsyncMock(return_value=mock_response)
                mock_client.return_value.post = mock_post

                await broker._refresh_access_token("test_refresh")

                call_kwargs = mock_post.call_args
                posted_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get(
                    "data"
                )
                scope_str = posted_data["scope"]
                assert "offline_access" not in scope_str
        await broker.close()

    async def test_master_token_refresh_omits_offline_access_for_cognito(
        self, mock_storage
    ):
        """refresh_master_token also respects IdP discovery."""
        broker = _make_broker(mock_storage)
        discovery = {
            "token_endpoint": "https://idp.example.com/token",
            "scopes_supported": ["openid"],
        }

        mock_storage.get_refresh_token.return_value = {
            "refresh_token": "current_refresh",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "rotated_refresh",
            "expires_in": 3600,
        }

        with patch.object(broker, "_get_oidc_config", return_value=discovery):
            with patch.object(broker, "_get_http_client") as mock_client:
                mock_post = AsyncMock(return_value=mock_response)
                mock_client.return_value.post = mock_post

                await broker.refresh_master_token("user1")

                call_kwargs = mock_post.call_args
                posted_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get(
                    "data"
                )
                scope_str = posted_data["scope"]
                assert "offline_access" not in scope_str
        await broker.close()
