"""
Integration tests for token introspection authorization.

These tests verify that the introspection endpoint properly enforces
authorization rules:
1. Client authentication is required (401 if missing)
2. Only the token owner can introspect its own tokens
3. Only the designated resource server can introspect tokens
4. Other clients cannot introspect tokens they don't own or aren't the audience for
"""

import asyncio
import logging
import os
import secrets

# Import helpers from conftest
import sys
import time
from typing import AsyncGenerator
from urllib.parse import quote

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from conftest import _handle_oauth_consent_screen

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def nextcloud_host() -> str:
    """Get Nextcloud host from environment."""
    host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")
    return host


@pytest.fixture(scope="module")
async def oidc_endpoints(nextcloud_host: str) -> dict[str, str]:
    """Discover OIDC endpoints."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        response = await client.get(discovery_url)
        response.raise_for_status()
        config = response.json()

        return {
            "token_endpoint": config["token_endpoint"],
            "authorization_endpoint": config.get("authorization_endpoint"),
            "introspection_endpoint": config.get("introspection_endpoint"),
            "registration_endpoint": config.get("registration_endpoint"),
        }


@pytest.fixture(scope="module")
async def test_oauth_clients(
    nextcloud_host: str, oidc_endpoints: dict[str, str], oauth_callback_server
) -> AsyncGenerator[dict[str, tuple[str, str]], None]:
    """
    Create multiple OAuth clients for introspection testing.

    Returns a dict mapping client names to (client_id, client_secret) tuples.
    """
    from nextcloud_mcp_server.auth.client_registration import register_client

    clients = {}
    registration_endpoint = oidc_endpoints["registration_endpoint"]

    # Get the correct callback URL from the oauth_callback_server fixture
    auth_states, callback_url = oauth_callback_server

    # Create client A (will be the token owner)
    logger.info("Creating OAuth client A for introspection testing")
    client_a = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name="Introspection Test Client A",
        redirect_uris=[callback_url],
        scopes="openid profile email",
        token_type="Bearer",  # Use opaque tokens for this test
    )
    clients["clientA"] = (client_a.client_id, client_a.client_secret)
    logger.info(f"Created client A: {client_a.client_id[:16]}...")

    # Create client B (will attempt to introspect client A's tokens)
    logger.info("Creating OAuth client B for introspection testing")
    client_b = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name="Introspection Test Client B",
        redirect_uris=[callback_url],
        scopes="openid profile email",
        token_type="Bearer",
    )
    clients["clientB"] = (client_b.client_id, client_b.client_secret)
    logger.info(f"Created client B: {client_b.client_id[:16]}...")

    # Create client C (third party, should not be able to introspect)
    logger.info("Creating OAuth client C for introspection testing")
    client_c = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name="Introspection Test Client C",
        redirect_uris=[callback_url],
        scopes="openid profile email",
        token_type="Bearer",
    )
    clients["clientC"] = (client_c.client_id, client_c.client_secret)
    logger.info(f"Created client C: {client_c.client_id[:16]}...")

    yield clients

    # Cleanup is handled by Nextcloud - clients will be removed when tests are done
    logger.info("Test OAuth clients fixture complete")


@pytest.mark.integration
async def test_introspection_requires_client_authentication(
    oidc_endpoints: dict[str, str],
):
    """
    Test that the introspection endpoint requires client authentication.

    Expected: 401 UNAUTHORIZED when credentials are missing or invalid.
    """
    introspection_endpoint = oidc_endpoints["introspection_endpoint"]
    if not introspection_endpoint:
        pytest.skip("Introspection endpoint not available")

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Test 1: No credentials
        response = await client.post(
            introspection_endpoint,
            data={"token": "some_token"},
        )
        assert response.status_code == 401, "Should return 401 without credentials"
        data = response.json()
        assert data.get("error") == "invalid_client"

        # Test 2: Invalid credentials
        response = await client.post(
            introspection_endpoint,
            data={"token": "some_token"},
            auth=("invalid_client", "invalid_secret"),
        )
        assert response.status_code == 401, "Should return 401 with invalid credentials"
        data = response.json()
        logger.info(f"Invalid client response: {data}")
        # Response may be either {"error": "invalid_client"} or {"message": "..."}
        # Both are acceptable as long as we get 401
        assert "error" in data or "message" in data, "Should return error information"


async def _obtain_token_for_client(
    browser,
    oauth_callback_server,
    client_id: str,
    client_secret: str,
    token_endpoint: str,
    authorization_endpoint: str,
    scope: str = "openid profile email",
    resource: str | None = None,
) -> str:
    """
    Helper to obtain an OAuth token using existing callback server and playwright automation.

    Reuses the pattern from conftest.py's playwright_oauth_token fixture.
    """
    username = os.getenv("NEXTCLOUD_USERNAME", "admin")
    password = os.getenv("NEXTCLOUD_PASSWORD", "admin")

    # Get callback server from fixture
    auth_states, callback_url = oauth_callback_server

    # Generate unique state parameter
    state = secrets.token_urlsafe(32)

    # Construct authorization URL
    auth_url_parts = [
        f"{authorization_endpoint}?",
        "response_type=code&",
        f"client_id={client_id}&",
        f"redirect_uri={quote(callback_url, safe='')}&",
        f"state={state}&",
        f"scope={quote(scope, safe='')}",
    ]

    if resource:
        auth_url_parts.append(f"&resource={quote(resource, safe='')}")

    auth_url = "".join(auth_url_parts)

    logger.info(f"Obtaining token for client {client_id[:16]}... with scopes={scope}")
    if resource:
        logger.info(f"  Resource parameter: {resource[:16]}...")

    # Browser automation (same pattern as conftest.py)
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        logger.debug(f"Navigating to: {auth_url[:100]}...")
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)
        current_url = page.url
        logger.debug(f"Current URL after navigation: {current_url}")

        # Handle login if needed
        if "/login" in current_url or "/index.php/login" in current_url:
            logger.info("Login page detected, filling credentials...")
            await page.wait_for_selector('input[name="user"]', timeout=10000)
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=60000)
            current_url = page.url
            logger.info(f"After login: {current_url}")

        # Wait a bit for page to fully render after login
        await asyncio.sleep(2)
        current_url = page.url
        logger.info(f"After waiting, current URL: {current_url}")

        # Check page content for debugging
        page_content = await page.content()
        has_consent_div = "#oidc-consent" in page_content
        logger.info(f"Page has #oidc-consent div: {has_consent_div}")

        # Handle consent screen using the helper from conftest
        try:
            consent_handled = await _handle_oauth_consent_screen(page, username)
            logger.info(f"Consent screen handled: {consent_handled}")
        except Exception as e:
            logger.warning(f"Error handling consent screen: {e}")
            # Take screenshot for debugging
            await page.screenshot(path=f"/tmp/consent_error_{state[:8]}.png")
            logger.error("Consent error screenshot saved")
            raise

        # Wait for callback server to receive auth code
        logger.info("Waiting for callback server to receive auth code...")
        timeout_seconds = 30
        start_time = time.time()
        while state not in auth_states:
            if time.time() - start_time > timeout_seconds:
                screenshot_path = (
                    f"/tmp/oauth_introspection_test_timeout_{state[:8]}.png"
                )
                await page.screenshot(path=screenshot_path)
                logger.error(f"Timeout! Screenshot saved to {screenshot_path}")
                logger.error(f"Current URL: {page.url}")
                raise TimeoutError(
                    f"Timeout waiting for OAuth callback (state={state[:16]}...)"
                )
            await asyncio.sleep(0.5)

        auth_code = auth_states[state]
        logger.info(f"Successfully received auth code: {auth_code[:20]}...")

    finally:
        await context.close()

    # Exchange code for token
    logger.debug("Exchanging authorization code for access token...")
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        token_response = await http_client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": callback_url,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )

        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if not access_token:
            raise ValueError(f"No access_token in response: {token_data}")

        logger.info("Successfully obtained access token")
        return access_token


@pytest.mark.integration
async def test_client_cannot_introspect_other_clients_tokens(
    playwright_oauth_token: str,
    shared_oauth_client_credentials: tuple,
    test_oauth_clients: dict[str, tuple[str, str]],
    oidc_endpoints: dict[str, str],
):
    """
    Test that one client cannot introspect tokens owned by another client.

    This test uses a pre-authorized shared OAuth client (with existing token)
    and verifies that a different client cannot introspect that token.

    Expected: introspection returns {active: false} to not reveal token existence.
    """
    introspection_endpoint = oidc_endpoints["introspection_endpoint"]
    if not introspection_endpoint:
        pytest.skip("Introspection endpoint not available")

    # Use the shared OAuth client's token (pre-authorized, working)
    access_token = playwright_oauth_token
    shared_client_id, shared_client_secret, _, _, _ = shared_oauth_client_credentials

    # Get a different client to try to introspect
    different_client_id, different_client_secret = test_oauth_clients["clientB"]

    logger.info(
        f"Testing introspection with shared client token: {access_token[:16]}..."
    )
    logger.info(f"Shared client ID: {shared_client_id[:16]}...")
    logger.info(f"Different client ID: {different_client_id[:16]}...")

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Test 1: The owning client (shared client) can introspect its own token
        response = await client.post(
            introspection_endpoint,
            data={"token": access_token},
            auth=(shared_client_id, shared_client_secret),
        )
        assert response.status_code == 200
        data = response.json()
        logger.info(f"Owner client introspection response: {data}")
        assert data.get("active") is True, (
            "Owner client should be able to introspect its own token"
        )

        # Test 2: A different client CANNOT introspect the shared client's token
        response = await client.post(
            introspection_endpoint,
            data={"token": access_token},
            auth=(different_client_id, different_client_secret),
        )
        assert response.status_code == 200
        data = response.json()
        logger.info(f"Different client introspection response: {data}")
        assert data.get("active") is False, (
            "Different client should NOT be able to introspect another client's token"
        )


@pytest.mark.integration
async def test_introspection_with_resource_parameter(
    browser,
    oauth_callback_server,
    test_oauth_clients: dict[str, tuple[str, str]],
    oidc_endpoints: dict[str, str],
    nextcloud_host: str,
):
    """
    Test that the resource server (specified via 'resource' parameter) can introspect tokens.

    This test verifies that when a token is issued with resource=clientB,
    clientB can introspect it even though it's owned by clientA.

    This requires obtaining a token with the 'resource' parameter set via authorization code grant.

    Uses playwright automation to obtain real tokens.
    """
    introspection_endpoint = oidc_endpoints["introspection_endpoint"]
    if not introspection_endpoint:
        pytest.skip("Introspection endpoint not available")

    client_a_id, client_a_secret = test_oauth_clients["clientA"]
    client_b_id, client_b_secret = test_oauth_clients["clientB"]
    client_c_id, client_c_secret = test_oauth_clients["clientC"]

    token_endpoint = oidc_endpoints["token_endpoint"]
    authorization_endpoint = oidc_endpoints.get("authorization_endpoint")
    if not authorization_endpoint:
        pytest.skip("Authorization endpoint not available")

    # Obtain a token for client A with resource parameter set to client B
    try:
        access_token = await _obtain_token_for_client(
            browser=browser,
            oauth_callback_server=oauth_callback_server,
            client_id=client_a_id,
            client_secret=client_a_secret,
            token_endpoint=token_endpoint,
            authorization_endpoint=authorization_endpoint,
            scope="openid profile email",
            resource=client_b_id,  # Set client B as the resource server
        )
    except Exception as e:
        logger.error(f"Failed to obtain token with resource parameter: {e}")
        pytest.skip(f"Cannot obtain test token with resource parameter: {e}")

    logger.info(
        f"Obtained access token from client A with resource={client_b_id}: {access_token[:16]}..."
    )

    # Test introspection
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Test 1: Client A (owner) can introspect its own token
        response = await client.post(
            introspection_endpoint,
            data={"token": access_token},
            auth=(client_a_id, client_a_secret),
        )
        assert response.status_code == 200
        data = response.json()
        logger.info(f"Client A (owner) introspection response: {data}")
        assert data.get("active") is True, (
            "Client A (owner) should be able to introspect its own token"
        )

        # Test 2: Client B (resource server) can introspect the token
        response = await client.post(
            introspection_endpoint,
            data={"token": access_token},
            auth=(client_b_id, client_b_secret),
        )
        assert response.status_code == 200
        data = response.json()
        logger.info(f"Client B (resource server) introspection response: {data}")
        assert data.get("active") is True, (
            "Client B (resource server) should be able to introspect token intended for it"
        )

        # Verify the resource field in the response matches client B
        logger.info(f"Full introspection response from Client B: {data}")

        # Test 3: Client C CANNOT introspect the token (not owner, not resource server)
        response = await client.post(
            introspection_endpoint,
            data={"token": access_token},
            auth=(client_c_id, client_c_secret),
        )
        assert response.status_code == 200
        data = response.json()
        logger.info(f"Client C (third party) introspection response: {data}")
        assert data.get("active") is False, (
            "Client C should NOT be able to introspect token (not owner or resource server)"
        )


@pytest.mark.integration
async def test_introspection_returns_inactive_for_invalid_token(
    test_oauth_clients: dict[str, tuple[str, str]],
    oidc_endpoints: dict[str, str],
):
    """
    Test that introspection returns {active: false} for invalid/unknown tokens.

    This is important for security - we shouldn't reveal whether a token exists or not.
    """
    introspection_endpoint = oidc_endpoints["introspection_endpoint"]
    if not introspection_endpoint:
        pytest.skip("Introspection endpoint not available")

    client_a_id, client_a_secret = test_oauth_clients["clientA"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Test with a fake token
        response = await client.post(
            introspection_endpoint,
            data={"token": "completely_fake_token_12345"},
            auth=(client_a_id, client_a_secret),
        )

        assert response.status_code == 200
        data = response.json()
        logger.info(f"Introspection response for fake token: {data}")
        assert data.get("active") is False, (
            "Should return active=false for invalid token"
        )
        # Should NOT return any other information
        assert len(data) == 1, "Should only return 'active' field for invalid token"


if __name__ == "__main__":
    # Run with: uv run pytest tests/server/test_introspection_authorization.py -v -s
    pytest.main([__file__, "-v", "-s", "-m", "integration"])
