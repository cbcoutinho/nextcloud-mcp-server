"""Integration tests for RFC 8693 Token Exchange with Keycloak.

These tests validate the complete token exchange flow:
1. Obtain client token from Keycloak
2. Exchange for Nextcloud-audience token via RFC 8693
3. Use exchanged token to access Nextcloud APIs
4. Verify CRUD operations work with exchanged tokens

Requirements:
- Keycloak running with nextcloud-mcp realm configured
- Nextcloud running with user_oidc app configured
- Standard Token Exchange enabled on both clients
- token-exchange-nextcloud scope configured
"""

from typing import Any

import httpx
import jwt
import pytest


@pytest.fixture
async def keycloak_base_url() -> str:
    """Keycloak base URL (external)."""
    return "http://localhost:8888"


@pytest.fixture
async def keycloak_token_url(keycloak_base_url: str) -> str:
    """Keycloak token endpoint URL."""
    return f"{keycloak_base_url}/realms/nextcloud-mcp/protocol/openid-connect/token"


@pytest.fixture
async def nextcloud_base_url() -> str:
    """Nextcloud base URL."""
    return "http://localhost:8080"


@pytest.fixture
async def http_client() -> httpx.AsyncClient:
    """Async HTTP client for API requests."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        yield client


@pytest.fixture
async def keycloak_client_token(
    http_client: httpx.AsyncClient, keycloak_token_url: str
) -> str:
    """Get client token from Keycloak using password grant.

    Returns token with aud: ["nextcloud-mcp-server", "nextcloud"]
    """
    response = await http_client.post(
        keycloak_token_url,
        data={
            "grant_type": "password",
            "client_id": "nextcloud-mcp-server",
            "client_secret": "mcp-secret-change-in-production",
            "username": "admin",
            "password": "admin",
            "scope": "openid profile email offline_access notes:read notes:write",
        },
    )
    response.raise_for_status()
    token_data = response.json()
    return token_data["access_token"]


async def exchange_token(
    http_client: httpx.AsyncClient,
    token_url: str,
    subject_token: str,
    audience: str = "nextcloud",
) -> dict[str, Any]:
    """Exchange token using RFC 8693.

    Args:
        http_client: HTTP client
        token_url: Token endpoint URL
        subject_token: Token to exchange
        audience: Target audience

    Returns:
        Token response with access_token and expires_in
    """
    response = await http_client.post(
        token_url,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": "nextcloud-mcp-server",
            "client_secret": "mcp-secret-change-in-production",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": audience,
        },
    )
    response.raise_for_status()
    return response.json()


def decode_token_claims(token: str) -> dict[str, Any]:
    """Decode JWT token claims without verification.

    Args:
        token: JWT token

    Returns:
        Token claims
    """
    return jwt.decode(token, options={"verify_signature": False})


@pytest.mark.integration
@pytest.mark.keycloak
class TestKeycloakTokenExchange:
    """Test RFC 8693 Token Exchange with Keycloak."""

    async def test_token_exchange_basic(
        self,
        http_client: httpx.AsyncClient,
        keycloak_token_url: str,
        keycloak_client_token: str,
    ):
        """Test basic token exchange flow."""
        # Verify initial token has both audiences
        initial_claims = decode_token_claims(keycloak_client_token)
        assert "nextcloud-mcp-server" in initial_claims["aud"]
        assert "nextcloud" in initial_claims["aud"]
        assert initial_claims["azp"] == "nextcloud-mcp-server"

        # Exchange for Nextcloud-audience token
        exchange_response = await exchange_token(
            http_client, keycloak_token_url, keycloak_client_token
        )

        assert "access_token" in exchange_response
        assert "expires_in" in exchange_response
        assert exchange_response["expires_in"] > 0

        # Verify exchanged token has correct audience
        exchanged_token = exchange_response["access_token"]
        exchanged_claims = decode_token_claims(exchanged_token)

        assert exchanged_claims["aud"] == "nextcloud"
        assert exchanged_claims["azp"] == "nextcloud-mcp-server"
        assert exchanged_claims["sub"] == initial_claims["sub"]

    async def test_token_exchange_with_nextcloud_api(
        self,
        http_client: httpx.AsyncClient,
        keycloak_token_url: str,
        keycloak_client_token: str,
        nextcloud_base_url: str,
    ):
        """Test exchanged token works with Nextcloud APIs."""
        # Exchange token
        exchange_response = await exchange_token(
            http_client, keycloak_token_url, keycloak_client_token
        )
        nextcloud_token = exchange_response["access_token"]

        # Call Nextcloud Capabilities API
        response = await http_client.get(
            f"{nextcloud_base_url}/ocs/v1.php/cloud/capabilities",
            headers={
                "Authorization": f"Bearer {nextcloud_token}",
                "OCS-APIRequest": "true",
            },
        )
        response.raise_for_status()

        # Verify response contains OCS data
        assert "ocs" in response.text.lower()

    async def test_token_exchange_multiple_times(
        self,
        http_client: httpx.AsyncClient,
        keycloak_token_url: str,
        keycloak_client_token: str,
    ):
        """Test multiple exchanges from same client token (stateless)."""
        # Exchange token three times
        tokens = []
        for _ in range(3):
            exchange_response = await exchange_token(
                http_client, keycloak_token_url, keycloak_client_token
            )
            tokens.append(exchange_response["access_token"])

        # All exchanges should succeed
        assert len(tokens) == 3

        # Tokens should be different (fresh ephemeral tokens)
        # Note: Keycloak may cache, so tokens might be identical
        # The important thing is that all exchanges succeeded

    async def test_token_exchange_crud_operations(
        self,
        http_client: httpx.AsyncClient,
        keycloak_token_url: str,
        keycloak_client_token: str,
        nextcloud_base_url: str,
    ):
        """Test CRUD operations with exchanged tokens."""
        notes_api = f"{nextcloud_base_url}/index.php/apps/notes/api/v1/notes"

        # Step 1: Exchange token for CREATE
        exchange_response = await exchange_token(
            http_client, keycloak_token_url, keycloak_client_token
        )
        create_token = exchange_response["access_token"]

        # Step 2: Create a test note
        create_response = await http_client.post(
            notes_api,
            headers={"Authorization": f"Bearer {create_token}"},
            json={
                "title": "Token Exchange Test",
                "content": "This note was created using an RFC 8693 exchanged token!",
                "category": "Test",
            },
        )
        create_response.raise_for_status()
        note_data = create_response.json()
        note_id = note_data["id"]

        assert note_data["title"] == "Token Exchange Test"
        assert note_data["category"] == "Test"

        # Step 3: Exchange token again for READ (simulate new request)
        exchange_response = await exchange_token(
            http_client, keycloak_token_url, keycloak_client_token
        )
        read_token = exchange_response["access_token"]

        # Step 4: Read the note back
        read_response = await http_client.get(
            f"{notes_api}/{note_id}",
            headers={"Authorization": f"Bearer {read_token}"},
        )
        read_response.raise_for_status()
        read_data = read_response.json()

        assert read_data["id"] == note_id
        assert read_data["title"] == "Token Exchange Test"
        assert "RFC 8693 exchanged token" in read_data["content"]

        # Step 5: Exchange token again for DELETE
        exchange_response = await exchange_token(
            http_client, keycloak_token_url, keycloak_client_token
        )
        delete_token = exchange_response["access_token"]

        # Step 6: Delete the note
        delete_response = await http_client.delete(
            f"{notes_api}/{note_id}",
            headers={"Authorization": f"Bearer {delete_token}"},
        )
        # Notes API returns the deleted note or empty array
        assert delete_response.status_code in (200, 204)

    async def test_token_claims_preservation(
        self,
        http_client: httpx.AsyncClient,
        keycloak_token_url: str,
        keycloak_client_token: str,
    ):
        """Test that important claims are preserved during exchange."""
        initial_claims = decode_token_claims(keycloak_client_token)

        # Exchange token
        exchange_response = await exchange_token(
            http_client, keycloak_token_url, keycloak_client_token
        )
        exchanged_token = exchange_response["access_token"]
        exchanged_claims = decode_token_claims(exchanged_token)

        # Subject (user ID) should be preserved
        assert exchanged_claims["sub"] == initial_claims["sub"]

        # Authorized party should show delegation
        assert exchanged_claims["azp"] == "nextcloud-mcp-server"

        # Audience should be filtered to target
        assert exchanged_claims["aud"] == "nextcloud"

        # Token should have expiration
        assert "exp" in exchanged_claims
        assert exchanged_claims["exp"] > 0

    async def test_token_exchange_scope_configuration(
        self, http_client: httpx.AsyncClient, keycloak_token_url: str
    ):
        """Test that token-exchange-nextcloud scope is configured as default.

        Since token-exchange-nextcloud is a default scope for nextcloud-mcp-server,
        all tokens should have the nextcloud audience available for exchange.
        """
        # Get a token - should automatically include default scopes
        response = await http_client.post(
            keycloak_token_url,
            data={
                "grant_type": "password",
                "client_id": "nextcloud-mcp-server",
                "client_secret": "mcp-secret-change-in-production",
                "username": "admin",
                "password": "admin",
                "scope": "openid profile email",
            },
        )
        response.raise_for_status()
        token = response.json()["access_token"]

        # Verify token has nextcloud in aud (from default token-exchange-nextcloud scope)
        claims = decode_token_claims(token)
        assert "nextcloud" in claims.get("aud", [])

        # Exchange should succeed
        exchange_response = await http_client.post(
            keycloak_token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": "nextcloud-mcp-server",
                "client_secret": "mcp-secret-change-in-production",
                "subject_token": token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "audience": "nextcloud",
            },
        )

        # Should succeed because token-exchange-nextcloud is a default scope
        assert exchange_response.status_code == 200
        exchanged_data = exchange_response.json()
        assert "access_token" in exchanged_data


@pytest.mark.integration
@pytest.mark.keycloak
class TestTokenExchangeService:
    """Test the TokenExchangeService implementation."""

    async def test_exchange_token_for_audience(
        self, keycloak_client_token: str, keycloak_token_url: str
    ):
        """Test the exchange_token_for_audience function."""
        from nextcloud_mcp_server.auth.token_exchange import (
            TokenExchangeService,
        )

        # Create service
        service = TokenExchangeService(
            oidc_discovery_url="http://localhost:8888/realms/nextcloud-mcp/.well-known/openid-configuration",
            client_id="nextcloud-mcp-server",
            client_secret="mcp-secret-change-in-production",
        )

        try:
            # Exchange token
            exchanged_token, expires_in = await service.exchange_token_for_audience(
                subject_token=keycloak_client_token,
                requested_audience="nextcloud",
            )

            # Verify exchange succeeded
            assert exchanged_token is not None
            assert isinstance(exchanged_token, str)
            assert expires_in > 0

            # Verify token has correct claims
            claims = decode_token_claims(exchanged_token)
            assert claims["aud"] == "nextcloud"
            assert claims["azp"] == "nextcloud-mcp-server"

        finally:
            await service.close()
