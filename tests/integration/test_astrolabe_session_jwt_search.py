"""Astrolabe session-derived JWT search path (card 120 auth refactor).

Cross-system interface test. The astrolabe app was refactored to mint a
short-lived JWT for the current Nextcloud session user on demand (via the
`oidc` app's ``TokenGenerationRequestEvent``), replacing the old OAuth
authorize + offline_access + stored-refresh-token flow. This test proves the
new path end-to-end:

    logged-in NC user → GET /apps/astrolabe/api/search → astrolabe mints a JWT
    (McpTokenMinter) → calls the MCP server with ``Authorization: Bearer`` →
    MCP validates the JWT (unified_verifier, aud=astrolabe_client_id) → results.

The headline behavioural change is that a user needs **no provisioning** to
search: there is no authorize redirect and ``has_background_access`` stays
False (app-password provisioning is now only for *background indexing*, a
separate opt-in covered by the background-sync tests).

Astrolabe is installed + configured (astrolabe_client_id, mcp_server_url) by
the container app-hooks; the test skips if that wiring is absent. Driven over
HTTP with BasicAuth (which establishes a Nextcloud session for the request) —
no browser needed.
"""

import logging
import os

import anyio
import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]

logger = logging.getLogger(__name__)

NEXTCLOUD_URL = "http://localhost:8080"
ASTROLABE_API = f"{NEXTCLOUD_URL}/apps/astrolabe/api"
_HEADERS = {"OCS-APIRequest": "true"}

# The first /search after container start is slow: astrolabe mints a JWT and
# the MCP server runs a semantic search that may cold-load the embedding model.
# A single 30s read timeout was a CI flake (httpx.ReadTimeout); give the search
# path a generous budget and one retry on transient transport errors.
_SEARCH_TIMEOUT = httpx.Timeout(90.0)


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, *, max_attempts: int = 2, **kwargs
) -> httpx.Response:
    """GET, retrying on transient transport errors (timeouts/conn resets)."""
    last_exc: httpx.TransportError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await client.get(url, **kwargs)
        except httpx.TransportError as e:  # covers timeouts + connect/read errors
            last_exc = e
            logger.warning(
                "GET %s failed (attempt %s/%s): %s", url, attempt, max_attempts, e
            )
            if attempt < max_attempts:
                await anyio.sleep(2)  # no point sleeping before we give up
    assert last_exc is not None  # loop ran at least once, so this is set
    raise last_exc


async def _astrolabe_configured(client: httpx.AsyncClient, auth) -> bool:
    """Readiness probe: astrolabe must be able to reach its MCP server."""
    try:
        resp = await client.get(
            f"{ASTROLABE_API}/vector-status", auth=auth, headers=_HEADERS
        )
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    return bool(resp.json().get("success"))


@pytest.mark.timeout(300)  # cold model load + retry can exceed the 180s default
async def test_session_user_searches_without_provisioning(test_users_setup):
    """A non-admin session user searches with no OAuth/provisioning step.

    success=True proves astrolabe minted a JWT from the session and the MCP
    server accepted it. Results may be empty (nothing indexed) — the auth
    chain, not recall, is under test here.
    """
    auth = httpx.BasicAuth("bob", test_users_setup["bob"]["password"])
    async with httpx.AsyncClient(timeout=30) as client:
        if not await _astrolabe_configured(client, auth):
            pytest.skip("Astrolabe not wired to an MCP server in this stack")

        # No provisioning: search must work purely from the session JWT.
        status = await client.get(
            f"{ASTROLABE_API}/v1/background-sync/status", auth=auth, headers=_HEADERS
        )
        assert status.json()["has_background_access"] is False, (
            "precondition: bob has not opted into background indexing"
        )

        resp = await _get_with_retry(
            client,
            f"{ASTROLABE_API}/search",
            params={"query": "quarterly planning", "limit": 3},
            auth=auth,
            headers=_HEADERS,
            timeout=_SEARCH_TIMEOUT,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True, (
        f"session-JWT search must succeed without provisioning; got {body}"
    )
    assert "results" in body and "algorithm_used" in body


@pytest.mark.timeout(300)  # cold model load + retry can exceed the 180s default
async def test_admin_session_search_succeeds():
    """The same JWT-mint path works for the admin session user."""
    admin_pw = os.environ["NEXTCLOUD_PASSWORD"]
    auth = httpx.BasicAuth(os.environ["NEXTCLOUD_USERNAME"], admin_pw)
    async with httpx.AsyncClient(timeout=30) as client:
        if not await _astrolabe_configured(client, auth):
            pytest.skip("Astrolabe not wired to an MCP server in this stack")
        resp = await _get_with_retry(
            client,
            f"{ASTROLABE_API}/search",
            params={"query": "infrastructure", "limit": 3},
            auth=auth,
            headers=_HEADERS,
            timeout=_SEARCH_TIMEOUT,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


async def test_search_requires_authentication():
    """Unauthenticated search is rejected (no anonymous JWT minting)."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        resp = await client.get(
            f"{ASTROLABE_API}/search",
            params={"query": "x"},
            headers=_HEADERS,
        )
    assert resp.status_code in (401, 302, 303, 307, 308), resp.status_code
