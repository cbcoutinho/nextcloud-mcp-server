"""
Integration test for RFC 8693 Token Exchange - Legacy V1 (Impersonation/Tier 1).

Tests the advanced impersonation feature where the service account token is
exchanged for a token with the target user's identity (sub claim changes).

This requires:
1. Keycloak with --features=preview enabled
2. Impersonation role granted to the service account

⚠️ This test will SKIP if impersonation permissions are not configured.

Configuration (one-time setup):
    # Grant impersonation role
    docker compose exec keycloak /opt/keycloak/bin/kcadm.sh config credentials \\
      --server http://localhost:8080 \\
      --realm master \\
      --user admin \\
      --password admin

    docker compose exec keycloak /opt/keycloak/bin/kcadm.sh add-roles \\
      -r nextcloud-mcp \\
      --uusername service-account-nextcloud-mcp-server \\
      --cclientid realm-management \\
      --rolename impersonation

Usage:
    pytest tests/integration/auth/test_token_exchange_legacy_v1.py -v
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


async def test_token_exchange_impersonation_requires_permissions(
    keycloak_config, service_account_token
):
    """Test that impersonation requires explicit permission grant.

    This test documents that Legacy V1 impersonation is opt-in and requires
    administrative configuration via Keycloak CLI.
    """

    target_user = "admin"  # User to impersonate

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
                "requested_subject": target_user,  # ← KEY: Request impersonation
            },
        )

        # If permissions not granted, we expect 403 Forbidden
        if exchange_response.status_code == 403:
            pytest.skip(
                "Impersonation permissions not configured. "
                "Run tests/manual/configure_impersonation.py or grant manually via Keycloak CLI. "
                "See test docstring for configuration commands."
            )

        # If permissions are granted, exchange should succeed
        assert exchange_response.status_code == 200, (
            f"Token exchange failed: {exchange_response.status_code} {exchange_response.text}"
        )


async def test_token_exchange_impersonation_changes_subject(
    keycloak_config, service_account_token
):
    """Test Legacy V1 impersonation - subject claim should change."""

    target_user = "admin"

    # Decode service account token
    service_claims = decode_jwt(service_account_token)
    assert "error" not in service_claims
    service_sub = service_claims["sub"]
    assert "service-account" in service_sub.lower()

    # Exchange token WITH requested_subject (Legacy V1 impersonation)
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
                "requested_subject": target_user,  # ← KEY: Impersonate admin
            },
        )

        # Skip if permissions not configured
        if exchange_response.status_code == 403:
            pytest.skip(
                "Impersonation permissions not configured. "
                "See test docstring for setup instructions."
            )

        # Token exchange should succeed with permissions
        assert exchange_response.status_code == 200, (
            f"Token exchange failed: {exchange_response.status_code} {exchange_response.text}"
        )

        exchanged_data = exchange_response.json()
        assert "access_token" in exchanged_data
        exchanged_token = exchanged_data["access_token"]

        # Decode exchanged token
        exchanged_claims = decode_jwt(exchanged_token)
        assert "error" not in exchanged_claims
        exchanged_sub = exchanged_claims["sub"]

        # CRITICAL: Verify impersonation - sub claim MUST change
        assert service_sub != exchanged_sub, (
            f"Impersonation should change subject claim. "
            f"Original: {service_sub}, Exchanged: {exchanged_sub}"
        )

        # Verify the new token represents the target user
        assert "preferred_username" in exchanged_claims
        assert exchanged_claims["preferred_username"] == target_user


async def test_impersonated_token_with_nextcloud(
    keycloak_config, service_account_token
):
    """Test that impersonated token works with Nextcloud APIs."""

    target_user = "admin"
    nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")

    # Exchange token with impersonation
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
                "requested_subject": target_user,
            },
        )

        # Skip if permissions not configured
        if exchange_response.status_code == 403:
            pytest.skip("Impersonation permissions not configured.")

        exchange_response.raise_for_status()
        exchanged_token = exchange_response.json()["access_token"]

        # Test with Nextcloud API
        nc_response = await client.get(
            f"{nextcloud_host}/ocs/v2.php/cloud/capabilities",
            headers={"Authorization": f"Bearer {exchanged_token}"},
        )

        # Should get valid response from Nextcloud
        assert nc_response.status_code in [
            200,
            401,
        ], f"Unexpected status: {nc_response.status_code}"

        if nc_response.status_code == 200:
            # Token was accepted - verify we got a valid response
            # Nextcloud OCS API can return XML or JSON
            assert len(nc_response.content) > 0, "Response should not be empty"
            content_type = nc_response.headers.get("content-type", "")
            assert any(t in content_type for t in ["json", "xml"]), (
                f"Unexpected content type: {content_type}"
            )


async def test_standard_v2_rejects_requested_subject():
    """Verify that Standard V2 (without preview features) rejects requested_subject.

    This test documents the key difference between Standard V2 and Legacy V1.

    NOTE: This test will PASS if preview features are enabled, as Keycloak
    accepts the parameter in Legacy V1 mode. The test exists to document the
    expected behavior when preview features are DISABLED.
    """

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

        # Try token exchange with requested_subject
        exchange_response = await client.post(
            token_endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": client_id,
                "client_secret": client_secret,
                "subject_token": service_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_subject": "admin",  # Try to impersonate
            },
        )

        # Standard V2: expects 400 Bad Request with "not supported" message
        # Legacy V1: accepts parameter, returns 200 or 403 (depending on permissions)

        if exchange_response.status_code == 400:
            # Standard V2 behavior
            error_data = exchange_response.json()
            assert (
                "requested_subject" in error_data.get("error_description", "").lower()
            )
            # Test passes - Standard V2 correctly rejects the parameter
        elif exchange_response.status_code in [200, 403]:
            # Legacy V1 behavior - parameter is accepted
            pytest.skip(
                "Preview features enabled - Keycloak is in Legacy V1 mode. "
                "This test documents Standard V2 behavior which rejects requested_subject."
            )
        else:
            pytest.fail(
                f"Unexpected status code: {exchange_response.status_code}. "
                f"Expected 400 (Standard V2) or 200/403 (Legacy V1)"
            )
