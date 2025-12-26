"""Unit tests for RFC 8693 Token Exchange (ADR-004).

Tests the critical token exchange pattern that separates:
- Session tokens (ephemeral, on-demand)
- Background tokens (stored refresh tokens)
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_broker import TokenBrokerService
from nextcloud_mcp_server.auth.token_exchange import TokenExchangeService

pytestmark = pytest.mark.unit


@pytest.fixture
async def token_storage():
    """Create test token storage."""

    # Generate valid Fernet key
    encryption_key = Fernet.generate_key()

    # Create temporary database file
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    storage = RefreshTokenStorage(db_path=db_path, encryption_key=encryption_key)
    await storage.initialize()

    # Expose encryption key for tests that need to manually encrypt/decrypt
    storage._test_encryption_key = encryption_key

    yield storage

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
async def token_exchange_service(token_storage):
    """Create test token exchange service."""
    service = TokenExchangeService(
        oidc_discovery_url="http://test-idp/.well-known/openid-configuration",
        client_id="test-client",
        client_secret="test-secret",
        nextcloud_host="http://test-nextcloud",
    )
    service.storage = token_storage
    yield service
    await service.http_client.aclose()


@pytest.fixture
async def token_broker(token_storage):
    """Create test token broker service."""
    # Use the same encryption key as storage
    encryption_key = token_storage._test_encryption_key

    broker = TokenBrokerService(
        storage=token_storage,
        oidc_discovery_url="http://test-idp/.well-known/openid-configuration",
        nextcloud_host="http://test-nextcloud",
        encryption_key=encryption_key,
        cache_ttl=300,
        cache_early_refresh=30,
    )
    yield broker
    await broker.close()


def create_test_jwt(
    user_id: str = "testuser", audience: str = "mcp-server", expires_in: int = 3600
) -> str:
    """Create a test JWT token."""
    import time

    payload = {
        "sub": user_id,
        "aud": audience,
        "exp": int(time.time()) + expires_in,
        "iat": int(time.time()),
        "iss": "http://test-idp",
    }

    # For testing, we don't sign the token (uses 'none' algorithm)
    # In production, tokens would be properly signed
    return jwt.encode(payload, "", algorithm="none")


class TestTokenExchange:
    """Test RFC 8693 token exchange implementation."""

    async def test_validate_flow1_token_success(self, token_exchange_service):
        """Test validation of Flow 1 token with correct audience."""
        # Create token with correct audience
        flow1_token = create_test_jwt(audience="mcp-server")

        # Should not raise an exception
        await token_exchange_service._validate_flow1_token(flow1_token)

    async def test_validate_flow1_token_wrong_audience(self, token_exchange_service):
        """Test validation fails with wrong audience."""
        # Create token with wrong audience
        flow1_token = create_test_jwt(audience="nextcloud")

        with pytest.raises(ValueError, match="Invalid token audience"):
            await token_exchange_service._validate_flow1_token(flow1_token)

    async def test_validate_flow1_token_expired(self, token_exchange_service):
        """Test validation fails with expired token."""
        # Create expired token
        flow1_token = create_test_jwt(audience="mcp-server", expires_in=-3600)

        with pytest.raises(ValueError, match="Token has expired"):
            await token_exchange_service._validate_flow1_token(flow1_token)

    async def test_extract_user_id(self, token_exchange_service):
        """Test extraction of user ID from token."""
        flow1_token = create_test_jwt(user_id="alice")

        user_id = token_exchange_service._extract_user_id(flow1_token)
        assert user_id == "alice"

    async def test_check_provisioning_not_provisioned(self, token_exchange_service):
        """Test provisioning check when user not provisioned."""
        result = await token_exchange_service._check_provisioning("unknown_user")
        assert result is False

    async def test_check_provisioning_is_provisioned(
        self, token_exchange_service, token_storage
    ):
        """Test provisioning check when user is provisioned."""
        # Store a refresh token for user
        await token_storage.store_refresh_token(
            user_id="alice", refresh_token="encrypted_refresh_token", flow_type="flow2"
        )

        result = await token_exchange_service._check_provisioning("alice")
        assert result is True

    async def test_exchange_token_not_provisioned(self, token_exchange_service):
        """Test token exchange fails when user not provisioned."""
        flow1_token = create_test_jwt(user_id="unprovisioneduser")

        with pytest.raises(RuntimeError, match="Nextcloud access not provisioned"):
            await token_exchange_service.exchange_token_for_delegation(
                flow1_token=flow1_token,
                requested_scopes=["notes:read"],
                requested_audience="nextcloud",
            )

    async def test_exchange_token_with_fallback(
        self, token_exchange_service, token_storage
    ):
        """Test token exchange with refresh grant fallback."""
        # Store a refresh token for user
        await token_storage.store_refresh_token(
            user_id="alice", refresh_token="test_refresh_token", flow_type="flow2"
        )

        # Create Flow 1 token
        flow1_token = create_test_jwt(user_id="alice", audience="mcp-server")

        # Mock HTTP client for token endpoint
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "delegated_token_12345",
            "token_type": "Bearer",
            "expires_in": 300,  # 5 minutes
        }

        with patch.object(
            token_exchange_service.http_client, "post", return_value=mock_response
        ):
            # Mock discovery endpoint
            with patch.object(
                token_exchange_service,
                "_discover_endpoints",
                return_value={"token_endpoint": "http://test-idp/token"},
            ):
                # Perform exchange
                (
                    token,
                    expires_in,
                ) = await token_exchange_service.exchange_token_for_delegation(
                    flow1_token=flow1_token,
                    requested_scopes=["notes:read"],
                    requested_audience="nextcloud",
                )

                assert token == "delegated_token_12345"
                assert expires_in == 300


class TestTokenBroker:
    """Test Token Broker session/background separation."""

    async def test_get_session_token(self, token_broker, token_storage):
        """Test getting ephemeral session token via exchange."""
        # Store refresh token for user
        await token_storage.store_refresh_token(
            user_id="alice", refresh_token="test_refresh_token", flow_type="flow2"
        )

        # Create Flow 1 token
        flow1_token = create_test_jwt(user_id="alice", audience="mcp-server")

        # Mock token exchange
        with patch(
            "nextcloud_mcp_server.auth.token_broker.exchange_token_for_delegation",
            return_value=("ephemeral_token_xyz", 300),
        ):
            token = await token_broker.get_session_token(
                flow1_token=flow1_token,
                required_scopes=["notes:read"],
                requested_audience="nextcloud",
            )

            assert token == "ephemeral_token_xyz"

            # Verify token is NOT cached (ephemeral)
            cached = await token_broker.cache.get("alice")
            assert cached is None  # Should not be in cache

    async def test_get_background_token(self, token_broker, token_storage):
        """Test getting background token with stored refresh."""
        # Store encrypted refresh token for user
        from cryptography.fernet import Fernet

        # Use the same encryption key as token_storage/token_broker
        fernet = Fernet(token_storage._test_encryption_key)
        encrypted_token = fernet.encrypt(b"background_refresh_token").decode()

        await token_storage.store_refresh_token(
            user_id="alice", refresh_token=encrypted_token, flow_type="flow2"
        )

        # Mock OIDC config and token response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "background_token_abc",
            "token_type": "Bearer",
            "expires_in": 3600,  # 1 hour
        }

        with patch.object(
            token_broker,
            "_get_oidc_config",
            return_value={"token_endpoint": "http://test/token"},
        ):
            with patch.object(token_broker, "_get_http_client") as mock_client:
                mock_client.return_value.post = AsyncMock(return_value=mock_response)

                # Mock audience validation
                with patch.object(
                    token_broker, "_validate_token_audience", return_value=None
                ):
                    token = await token_broker.get_background_token(
                        user_id="alice", required_scopes=["notes:sync", "files:sync"]
                    )

                    assert token == "background_token_abc"

                    # Verify token IS cached (background tokens can be cached)
                    cache_key = "alice:background:files:sync,notes:sync"
                    cached = await token_broker.cache.get(cache_key)
                    assert cached == "background_token_abc"

    async def test_session_background_separation(self, token_broker, token_storage):
        """Test that session and background tokens are kept separate."""
        # Store refresh token
        from cryptography.fernet import Fernet

        # Use the same encryption key as token_storage/token_broker
        fernet = Fernet(token_storage._test_encryption_key)
        encrypted_token = fernet.encrypt(b"master_refresh_token").decode()

        await token_storage.store_refresh_token(
            user_id="alice", refresh_token=encrypted_token, flow_type="flow2"
        )

        flow1_token = create_test_jwt(user_id="alice", audience="mcp-server")

        # Mock different tokens for session vs background
        session_token = "ephemeral_session_123"
        background_token = "cached_background_456"

        # Get session token
        with patch(
            "nextcloud_mcp_server.auth.token_broker.exchange_token_for_delegation",
            return_value=(session_token, 300),
        ):
            session_result = await token_broker.get_session_token(
                flow1_token=flow1_token, required_scopes=["notes:read"]
            )
            assert session_result == session_token

        # Get background token
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": background_token,
            "expires_in": 3600,
        }

        with patch.object(
            token_broker,
            "_get_oidc_config",
            return_value={"token_endpoint": "http://test/token"},
        ):
            with patch.object(token_broker, "_get_http_client") as mock_client:
                mock_client.return_value.post = AsyncMock(return_value=mock_response)
                with patch.object(
                    token_broker, "_validate_token_audience", return_value=None
                ):
                    background_result = await token_broker.get_background_token(
                        user_id="alice", required_scopes=["notes:sync"]
                    )
                    assert background_result == background_token

        # Verify they are different tokens
        assert session_result != background_result

        # Verify session token not cached
        assert await token_broker.cache.get("alice") is None

        # Verify background token IS cached
        cache_key = "alice:background:notes:sync"
        assert await token_broker.cache.get(cache_key) == background_token


class TestScopeDownscoping:
    """Test that tokens request only necessary scopes."""

    async def test_session_token_minimal_scopes(
        self, token_exchange_service, token_storage
    ):
        """Test session tokens request minimal scopes."""
        # Store refresh token
        await token_storage.store_refresh_token(
            user_id="alice", refresh_token="test_refresh_token", flow_type="flow2"
        )

        flow1_token = create_test_jwt(user_id="alice", audience="mcp-server")

        # Track what scopes are requested
        requested_scopes = None

        async def mock_post(url, data, headers=None):
            nonlocal requested_scopes
            requested_scopes = data.get("scope", "").split()

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "access_token": "scoped_token",
                "expires_in": 300,
            }
            return mock_response

        with patch.object(
            token_exchange_service.http_client, "post", side_effect=mock_post
        ):
            with patch.object(
                token_exchange_service,
                "_discover_endpoints",
                return_value={"token_endpoint": "http://test/token"},
            ):
                await token_exchange_service.exchange_token_for_delegation(
                    flow1_token=flow1_token,
                    requested_scopes=["notes:read"],  # Only read scope
                    requested_audience="nextcloud",
                )

                # Verify only requested scope was included
                assert "notes:read" in requested_scopes
                assert "notes:write" not in requested_scopes
                assert "calendar:write" not in requested_scopes

    async def test_background_token_different_scopes(self, token_broker, token_storage):
        """Test background tokens can request different scopes than session."""
        from cryptography.fernet import Fernet

        # Use the same encryption key as token_storage/token_broker
        fernet = Fernet(token_storage._test_encryption_key)
        encrypted_token = fernet.encrypt(b"refresh_token").decode()

        await token_storage.store_refresh_token(
            user_id="alice", refresh_token=encrypted_token, flow_type="flow2"
        )

        # Track requested scopes
        requested_scopes = None

        async def mock_post(url, data, headers=None):
            nonlocal requested_scopes
            requested_scopes = data.get("scope", "").split()

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "access_token": "background_sync_token",
                "expires_in": 3600,
            }
            return mock_response

        with patch.object(
            token_broker,
            "_get_oidc_config",
            return_value={"token_endpoint": "http://test/token"},
        ):
            with patch.object(token_broker, "_get_http_client") as mock_client:
                mock_client.return_value.post = mock_post
                with patch.object(
                    token_broker, "_validate_token_audience", return_value=None
                ):
                    await token_broker.get_background_token(
                        user_id="alice",
                        required_scopes=["notes:sync", "files:sync", "calendar:sync"],
                    )

                    # Verify sync scopes were requested
                    assert "notes:sync" in requested_scopes
                    assert "files:sync" in requested_scopes
                    assert "calendar:sync" in requested_scopes
                    # Basic OIDC scopes should also be included
                    assert "openid" in requested_scopes
                    assert "profile" in requested_scopes
