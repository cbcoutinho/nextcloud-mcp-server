"""
Integration tests for DCR and static OIDC clients against the mcp-keycloak service.

The mcp-keycloak service uses Keycloak as an external IdP. Keycloak supports
Dynamic Client Registration (RFC 7591/7592), so the DCR proxy forwards
registrations to Keycloak and registers the resulting client locally.

Static clients configured via ALLOWED_MCP_CLIENTS should work for the
authorization flow without DCR.

Requires: docker compose --profile keycloak up --build -d
"""

import base64
import hashlib
import logging
import secrets

import httpx
import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.keycloak]

MCP_KEYCLOAK_BASE_URL = "http://localhost:8002"
KEYCLOAK_BASE_URL = "http://localhost:8888"
KEYCLOAK_REALM = "nextcloud-mcp"


@pytest.fixture(scope="module")
async def keycloak_mcp_available():
    """Check that the mcp-keycloak service is reachable and return AS metadata."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{MCP_KEYCLOAK_BASE_URL}/.well-known/oauth-authorization-server"
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.HTTPStatusError) as e:
            pytest.skip(f"mcp-keycloak service not available: {e}")


@pytest.fixture()
async def dcr_client(keycloak_mcp_available):
    """Register a client via DCR proxy and clean up after test."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        # Register via MCP proxy
        resp = await http.post(
            f"{MCP_KEYCLOAK_BASE_URL}/oauth/register",
            json={
                "client_name": "test-dcr-keycloak",
                "redirect_uris": ["http://localhost:9999/callback"],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_basic",
                "scope": "openid profile email",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    yield data

    # Cleanup: delete via Keycloak directly (proxy URI uses internal hostname)
    client_id = data.get("client_id")
    rat = data.get("registration_access_token")
    if client_id and rat:
        async with httpx.AsyncClient(timeout=10.0) as http:
            await http.delete(
                f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"
                f"/clients-registrations/openid-connect/{client_id}",
                headers={"Authorization": f"Bearer {rat}"},
            )


# --- DCR tests ---


async def test_dcr_proxy_registers_client(dcr_client):
    """DCR proxy should forward registration to Keycloak and return
    RFC 7591 response with client credentials."""
    assert "client_id" in dcr_client
    assert "client_secret" in dcr_client
    assert dcr_client["client_name"] == "test-dcr-keycloak"


async def test_dcr_proxy_returns_rfc7592_fields(dcr_client):
    """DCR response should include RFC 7592 management fields for
    client lifecycle management."""
    assert "registration_access_token" in dcr_client
    assert dcr_client["registration_access_token"]
    assert "registration_client_uri" in dcr_client
    assert dcr_client["registration_client_uri"]


async def test_dcr_client_accepted_by_authorize(keycloak_mcp_available, dcr_client):
    """A DCR-registered client should be accepted by the authorization endpoint
    (redirects to IdP login rather than returning an error)."""
    # Generate PKCE challenge (required by the server)
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as http:
        resp = await http.get(
            f"{MCP_KEYCLOAK_BASE_URL}/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": dcr_client["client_id"],
                "redirect_uri": "http://localhost:9999/callback",
                "state": "test-state",
                "scope": "openid",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

    # Should redirect to IdP (302) not error (400)
    assert resp.status_code == 302, (
        f"Expected redirect to IdP, got {resp.status_code}: {resp.text}"
    )


async def test_dcr_client_deletion_via_keycloak(keycloak_mcp_available):
    """Full DCR lifecycle: register via proxy, verify, delete via RFC 7592."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        # Register
        resp = await http.post(
            f"{MCP_KEYCLOAK_BASE_URL}/oauth/register",
            json={
                "client_name": "test-dcr-lifecycle",
                "redirect_uris": ["http://localhost:9999/callback"],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
            },
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        client_id = data["client_id"]
        rat = data["registration_access_token"]

        try:
            # Delete via Keycloak (external URL)
            del_resp = await http.delete(
                f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"
                f"/clients-registrations/openid-connect/{client_id}",
                headers={"Authorization": f"Bearer {rat}"},
            )
            assert del_resp.status_code == 204
        except Exception:
            # Best-effort cleanup if assertion failed
            try:
                await http.delete(
                    f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"
                    f"/clients-registrations/openid-connect/{client_id}",
                    headers={"Authorization": f"Bearer {rat}"},
                )
            except Exception:
                pass
            raise


# --- AS metadata tests ---


async def test_as_metadata_advertises_registration_endpoint(keycloak_mcp_available):
    """AS metadata should advertise /oauth/register for DCR discovery."""
    metadata = keycloak_mcp_available
    assert "registration_endpoint" in metadata
    assert metadata["registration_endpoint"].endswith("/oauth/register")


async def test_as_metadata_has_required_fields(keycloak_mcp_available):
    """Verify the AS metadata contains all RFC 8414 required fields."""
    metadata = keycloak_mcp_available
    required_fields = [
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "response_types_supported",
        "grant_types_supported",
        "code_challenge_methods_supported",
    ]
    for field in required_fields:
        assert field in metadata, f"Missing required field: {field}"


# --- Static client / unknown client tests ---


async def test_authorize_rejects_unknown_client(keycloak_mcp_available):
    """Authorization endpoint should reject client_ids that are neither
    statically configured nor dynamically registered."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as http:
        resp = await http.get(
            f"{MCP_KEYCLOAK_BASE_URL}/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "nonexistent-client-id",
                "redirect_uri": "http://localhost:9999/callback",
                "state": "test-state",
                "scope": "openid",
            },
        )

    # Should return an error (400 or redirect with error)
    assert resp.status_code in (400, 302)
    if resp.status_code == 400:
        data = resp.json()
        assert "error" in data
    else:
        # 302 redirect must carry an error parameter
        location = resp.headers.get("location", "")
        assert "error=" in location, (
            f"302 redirect should contain error= in Location, got: {location}"
        )
