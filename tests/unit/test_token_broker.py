"""
Unit tests for Token Broker Service (ADR-004 Progressive Consent).

Tests the token management, caching, and refresh logic without
requiring real network calls or database connections.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.token_broker import TokenBrokerService, TokenCache

pytestmark = pytest.mark.unit


@pytest.fixture
def encryption_key():
    """Generate test encryption key."""
    return Fernet.generate_key().decode()


@pytest.fixture
def mock_storage():
    """Mock RefreshTokenStorage."""
    storage = AsyncMock()
    storage.get_refresh_token = AsyncMock(return_value=None)
    storage.store_refresh_token = AsyncMock()
    storage.delete_refresh_token = AsyncMock()
    return storage


@pytest.fixture
def mock_oidc_config():
    """Mock OIDC configuration."""
    return {
        "issuer": "https://idp.example.com",
        "token_endpoint": "https://idp.example.com/token",
        "revocation_endpoint": "https://idp.example.com/revoke",
        "jwks_uri": "https://idp.example.com/jwks",
    }


@pytest.fixture
async def token_broker(mock_storage, encryption_key):
    """Create TokenBrokerService instance."""
    broker = TokenBrokerService(
        storage=mock_storage,
        oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
        nextcloud_host="https://nextcloud.example.com",
        encryption_key=encryption_key,
        cache_ttl=300,
    )
    yield broker
    await broker.close()


class TestTokenCache:
    """Test the TokenCache component."""

    async def test_cache_stores_and_retrieves_token(self):
        """Test basic cache storage and retrieval."""
        cache = TokenCache(ttl_seconds=60)

        # Store token with sufficient expiry time (more than 30s threshold)
        await cache.set("user1", "test_token", expires_in=120)

        # Retrieve token
        token = await cache.get("user1")
        assert token == "test_token"

    async def test_cache_respects_ttl(self):
        """Test that cache respects TTL."""
        # Create cache with 1 second TTL and 0 second early refresh
        cache = TokenCache(ttl_seconds=1, early_refresh_seconds=0)

        # Store token
        await cache.set("user1", "test_token")

        # Token should be available immediately
        assert await cache.get("user1") == "test_token"

        # Wait for TTL to expire
        await asyncio.sleep(1.1)

        # Token should be expired
        assert await cache.get("user1") is None

    async def test_cache_early_refresh(self):
        """Test that cache returns None for tokens expiring soon."""
        cache = TokenCache(ttl_seconds=60)

        # Store token that expires in 25 seconds (less than 30s threshold)
        await cache.set("user1", "test_token", expires_in=25)

        # Should return None as it's expiring soon (within 30s)
        assert await cache.get("user1") is None

    async def test_cache_invalidation(self):
        """Test cache invalidation."""
        cache = TokenCache(ttl_seconds=60)

        # Store and verify token
        await cache.set("user1", "test_token")
        assert await cache.get("user1") == "test_token"

        # Invalidate
        await cache.invalidate("user1")

        # Should be removed
        assert await cache.get("user1") is None


class TestTokenBrokerService:
    """Test the TokenBrokerService."""

    async def test_has_nextcloud_provisioning(self, token_broker, mock_storage):
        """Test checking if user has provisioned Nextcloud access."""
        # No provisioning
        mock_storage.get_refresh_token.return_value = None
        assert await token_broker.has_nextcloud_provisioning("user1") is False

        # Has provisioning
        mock_storage.get_refresh_token.return_value = {
            "refresh_token": "encrypted_token",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        }
        assert await token_broker.has_nextcloud_provisioning("user1") is True

    async def test_get_nextcloud_token_from_cache(self, token_broker):
        """Test getting token from cache."""
        # Pre-populate cache
        await token_broker.cache.set("user1", "cached_token", expires_in=300)

        # Should return cached token without calling storage
        token = await token_broker.get_nextcloud_token("user1")
        assert token == "cached_token"
        token_broker.storage.get_refresh_token.assert_not_called()

    async def test_get_nextcloud_token_refresh(
        self, token_broker, mock_storage, encryption_key, mock_oidc_config
    ):
        """Test getting token via refresh when not cached."""
        # Setup encrypted refresh token in storage
        fernet = Fernet(encryption_key.encode())
        encrypted_token = fernet.encrypt(b"test_refresh_token").decode()
        mock_storage.get_refresh_token.return_value = {
            "refresh_token": encrypted_token,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        }

        # Mock HTTP client for token refresh
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

        with patch.object(
            token_broker, "_get_oidc_config", return_value=mock_oidc_config
        ):
            with patch.object(token_broker, "_get_http_client") as mock_client:
                mock_client.return_value.post = AsyncMock(return_value=mock_response)

                # Get token (should refresh)
                token = await token_broker.get_nextcloud_token("user1")

                assert token == "new_access_token"
                # Verify token was cached
                cached = await token_broker.cache.get("user1")
                assert cached == "new_access_token"

    async def test_get_nextcloud_token_no_provisioning(
        self, token_broker, mock_storage
    ):
        """Test getting token when user hasn't provisioned."""
        mock_storage.get_refresh_token.return_value = None

        token = await token_broker.get_nextcloud_token("user1")
        assert token is None

    async def test_refresh_master_token(
        self, token_broker, mock_storage, encryption_key, mock_oidc_config
    ):
        """Test master refresh token rotation."""
        # Setup current refresh token
        fernet = Fernet(encryption_key.encode())
        encrypted_token = fernet.encrypt(b"current_refresh_token").decode()
        mock_storage.get_refresh_token.return_value = {
            "refresh_token": encrypted_token,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        }

        # Mock successful refresh response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }

        with patch.object(
            token_broker, "_get_oidc_config", return_value=mock_oidc_config
        ):
            with patch.object(token_broker, "_get_http_client") as mock_client:
                mock_client.return_value.post = AsyncMock(return_value=mock_response)

                # Rotate token
                success = await token_broker.refresh_master_token("user1")

                assert success is True
                # Verify new token was stored
                mock_storage.store_refresh_token.assert_called_once()
                call_args = mock_storage.store_refresh_token.call_args[1]
                assert call_args["user_id"] == "user1"
                # Decrypt to verify it's the new token
                stored_token = fernet.decrypt(
                    call_args["refresh_token"].encode()
                ).decode()
                assert stored_token == "new_refresh_token"

    async def test_refresh_master_token_no_rotation(
        self, token_broker, mock_storage, encryption_key, mock_oidc_config
    ):
        """Test when IdP returns same refresh token (no rotation)."""
        # Setup current refresh token
        fernet = Fernet(encryption_key.encode())
        encrypted_token = fernet.encrypt(b"same_refresh_token").decode()
        mock_storage.get_refresh_token.return_value = {
            "refresh_token": encrypted_token,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        }

        # Mock response with same refresh token
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "same_refresh_token",
            "expires_in": 3600,
        }

        with patch.object(
            token_broker, "_get_oidc_config", return_value=mock_oidc_config
        ):
            with patch.object(token_broker, "_get_http_client") as mock_client:
                mock_client.return_value.post = AsyncMock(return_value=mock_response)

                success = await token_broker.refresh_master_token("user1")

                assert success is True
                # Should not store if token didn't change
                mock_storage.store_refresh_token.assert_not_called()

    async def test_revoke_nextcloud_access(
        self, token_broker, mock_storage, encryption_key, mock_oidc_config
    ):
        """Test revoking Nextcloud access."""
        # Setup refresh token for revocation
        fernet = Fernet(encryption_key.encode())
        encrypted_token = fernet.encrypt(b"token_to_revoke").decode()
        mock_storage.get_refresh_token.return_value = {
            "refresh_token": encrypted_token,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        }

        # Mock revocation response
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(
            token_broker, "_get_oidc_config", return_value=mock_oidc_config
        ):
            with patch.object(token_broker, "_get_http_client") as mock_client:
                mock_client.return_value.post = AsyncMock(return_value=mock_response)

                # Pre-populate cache
                await token_broker.cache.set("user1", "cached_token")

                # Revoke access
                success = await token_broker.revoke_nextcloud_access("user1")

                assert success is True
                # Verify token was deleted from storage
                mock_storage.delete_refresh_token.assert_called_once_with("user1")
                # Verify cache was cleared
                assert await token_broker.cache.get("user1") is None

    async def test_validate_token_audience(self, token_broker):
        """Test token audience validation."""
        # Create test token with audience
        test_payload = {
            "sub": "user1",
            "aud": ["nextcloud", "other-service"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        test_token = jwt.encode(test_payload, "secret", algorithm="HS256")

        # Should not raise for correct audience
        await token_broker._validate_token_audience(test_token, "nextcloud")

        # Should raise for wrong audience
        with pytest.raises(ValueError, match="doesn't include wrong-audience"):
            await token_broker._validate_token_audience(test_token, "wrong-audience")

    async def test_token_refresh_with_network_error(
        self, token_broker, mock_storage, encryption_key
    ):
        """Test handling network errors during token refresh."""
        # Setup encrypted refresh token
        fernet = Fernet(encryption_key.encode())
        encrypted_token = fernet.encrypt(b"test_refresh_token").decode()
        mock_storage.get_refresh_token.return_value = {
            "refresh_token": encrypted_token,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        }

        # Mock network error
        with patch.object(token_broker, "_get_http_client") as mock_client:
            mock_client.return_value.post = AsyncMock(
                side_effect=httpx.NetworkError("Connection failed")
            )

            # Should return None on error
            token = await token_broker.get_nextcloud_token("user1")
            assert token is None

            # Cache should be invalidated
            assert await token_broker.cache.get("user1") is None

    async def test_concurrent_cache_access(self, token_broker):
        """Test concurrent access to token cache."""
        # Pre-populate cache
        await token_broker.cache.set("user1", "token1", expires_in=300)
        await token_broker.cache.set("user2", "token2", expires_in=300)

        # Concurrent reads
        results = await asyncio.gather(
            token_broker.cache.get("user1"),
            token_broker.cache.get("user2"),
            token_broker.cache.get("user1"),
            token_broker.cache.get("user2"),
        )

        assert results == ["token1", "token2", "token1", "token2"]
