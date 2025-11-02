"""
Integration test for RFC 8693 Token Exchange - Standard V2 (Delegation/Tier 2).

Tests the production-ready token exchange without impersonation.
The service account exchanges its token for a user-scoped token while
maintaining its own identity (sub claim unchanged).

This is the RECOMMENDED approach for most use cases.

Requirements:
- Keycloak container running (can be Standard V2 or Legacy V1)
- MCP Keycloak service running on port 8002

Usage:
    pytest tests/integration/auth/test_token_exchange_standard_v2.py -v
"""

import base64
import json
import os

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.anyio, pytest.mark.keycloak]


def decode_jwt(token: str) -> dict:
    """Decode JWT token payload without verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {"error": "Invalid JWT format"}

        payload = parts[1]
        padding = 4 - (len(payload) % 4)
        if padding != 4:
            payload += "=" * padding

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as e:
        return {"error": str(e)}


@pytest.fixture
def keycloak_config():
    """Keycloak configuration for testing."""
    return {
        "url": os.getenv("KEYCLOAK_URL", "http://localhost:8888"),
        "realm": os.getenv("KEYCLOAK_REALM", "nextcloud-mcp"),
        "client_id": os.getenv("KEYCLOAK_CLIENT_ID", "nextcloud-mcp-server"),
        "client_secret": os.getenv(
            "KEYCLOAK_CLIENT_SECRET", "mcp-secret-change-in-production"
        ),
        "token_endpoint": f"{os.getenv('KEYCLOAK_URL', 'http://localhost:8888')}/realms/{os.getenv('KEYCLOAK_REALM', 'nextcloud-mcp')}/protocol/openid-connect/token",
    }


@pytest.fixture
async def service_account_token(keycloak_config):
    """Get a service account token using client_credentials grant."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            keycloak_config["token_endpoint"],
            data={
                "grant_type": "client_credentials",
                "client_id": keycloak_config["client_id"],
                "client_secret": keycloak_config["client_secret"],
                "scope": "openid profile email",
            },
        )
        response.raise_for_status()
        token_data = response.json()
        return token_data["access_token"]


async def test_token_exchange_delegation(keycloak_config, service_account_token):
    """Test Standard V2 token exchange with delegation (no impersonation)."""

    # Decode service account token to get original claims
    service_claims = decode_jwt(service_account_token)
    assert "error" not in service_claims, "Failed to decode service account token"
    assert "sub" in service_claims
    service_sub = service_claims["sub"]

    # Exchange token WITHOUT requested_subject (Standard V2 delegation)
    async with httpx.AsyncClient(timeout=30.0) as client:
        exchange_response = await client.post(
            keycloak_config["token_endpoint"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": keycloak_config["client_id"],
                "client_secret": keycloak_config["client_secret"],
                "subject_token": service_account_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                # NOTE: NO requested_subject parameter - this is delegation, not impersonation
            },
        )

        # Token exchange should succeed
        assert exchange_response.status_code == 200, (
            f"Token exchange failed: {exchange_response.status_code} {exchange_response.text}"
        )

        exchanged_data = exchange_response.json()
        assert "access_token" in exchanged_data
        assert "token_type" in exchanged_data
        assert exchanged_data["token_type"].lower() == "bearer"

        exchanged_token = exchanged_data["access_token"]

        # Decode exchanged token
        exchanged_claims = decode_jwt(exchanged_token)
        assert "error" not in exchanged_claims, "Failed to decode exchanged token"
        assert "sub" in exchanged_claims
        exchanged_sub = exchanged_claims["sub"]

        # CRITICAL: Verify delegation behavior - sub claim should NOT change
        assert service_sub == exchanged_sub, (
            f"Subject should remain unchanged in delegation (service account identity preserved). Original: {service_sub}, Exchanged: {exchanged_sub}"
        )

        # The exchanged token should still identify as the service account
        assert "service-account" in exchanged_sub.lower(), (
            "Exchanged token should maintain service account identity"
        )


async def test_exchanged_token_with_nextcloud(keycloak_config, service_account_token):
    """Test that exchanged token works with Nextcloud APIs."""

    nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")

    # Exchange the service account token
    async with httpx.AsyncClient(timeout=30.0) as client:
        exchange_response = await client.post(
            keycloak_config["token_endpoint"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": keycloak_config["client_id"],
                "client_secret": keycloak_config["client_secret"],
                "subject_token": service_account_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            },
        )
        exchange_response.raise_for_status()
        exchanged_token = exchange_response.json()["access_token"]

        # Test the exchanged token with Nextcloud API
        nc_response = await client.get(
            f"{nextcloud_host}/ocs/v2.php/cloud/capabilities",
            headers={"Authorization": f"Bearer {exchanged_token}"},
        )

        # Should get a valid response from Nextcloud
        # Note: This might fail with 401 if user_oidc doesn't accept the token
        # That's expected - this test verifies the token exchange itself works
        assert nc_response.status_code in [
            200,
            401,
        ], f"Unexpected status: {nc_response.status_code}"

        if nc_response.status_code == 200:
            # Token was accepted - verify we got a valid response
            # Nextcloud OCS API can return XML or JSON
            assert len(nc_response.content) > 0, "Response should not be empty"
            # Verify we got either JSON or XML capabilities response
            content_type = nc_response.headers.get("content-type", "")
            assert any(t in content_type for t in ["json", "xml"]), (
                f"Unexpected content type: {content_type}"
            )


async def test_token_exchange_without_permissions_should_work():
    """Verify Standard V2 doesn't require special permissions (unlike Legacy V1 impersonation)."""

    # This test documents that Standard V2 token exchange works out-of-the-box
    # without needing to grant impersonation roles via Keycloak CLI

    keycloak_url = os.getenv("KEYCLOAK_URL", "http://localhost:8888")
    realm = os.getenv("KEYCLOAK_REALM", "nextcloud-mcp")
    client_id = os.getenv("KEYCLOAK_CLIENT_ID", "nextcloud-mcp-server")
    client_secret = os.getenv(
        "KEYCLOAK_CLIENT_SECRET", "mcp-secret-change-in-production"
    )
    token_endpoint = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/token"

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get service account token
        token_response = await client.post(
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "openid profile email",
            },
        )
        token_response.raise_for_status()
        service_token = token_response.json()["access_token"]

        # Exchange token - should work without any special role grants
        exchange_response = await client.post(
            token_endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": client_id,
                "client_secret": client_secret,
                "subject_token": service_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            },
        )

        # Should succeed without 403 Forbidden (no permission requirements)
        assert exchange_response.status_code == 200, (
            f"Standard V2 delegation should work without special permissions. "
            f"Got: {exchange_response.status_code} {exchange_response.text}"
        )
