"""Integration tests for Astrolabe personal-settings background-sync endpoints.

Cross-system interface test. The astrolabe app (installed by
app-hooks/post-installation/20-install-astrolabe-app.sh; source in
./third_party/astrolabe) was refactored to session-minted JWTs — the old
per-user OAuth flow and its ``/apps/astrolabe/oauth/disconnect`` route are
gone. Background indexing is now an app-password opt-in with a single revoke
endpoint.

These tests assert the *current* HTTP surface of the settings page:
- the revoke endpoint exists and is auth-gated
  (POST /apps/astrolabe/api/v1/background-sync/credentials/revoke)
- the obsolete OAuth disconnect route is gone (404)
- the personal settings page route resolves
"""

import httpx
import pytest

pytestmark = pytest.mark.integration

NEXTCLOUD_URL = "http://localhost:8080"
ASTROLABE = f"{NEXTCLOUD_URL}/apps/astrolabe"

# Auth failures (no session) surface as 401 or a login redirect.
_UNAUTH = {401, 302, 303, 307, 308}


async def test_revoke_endpoint_requires_auth():
    """The background-sync revoke endpoint exists and rejects anonymous calls."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.post(
            f"{ASTROLABE}/api/v1/background-sync/credentials/revoke",
            headers={"OCS-APIRequest": "true"},
        )
    # Must NOT be 404 — the route must exist — and must be auth-gated.
    assert resp.status_code != 404, "revoke route missing"
    assert resp.status_code in _UNAUTH, (
        f"expected auth rejection, got {resp.status_code}"
    )


async def test_obsolete_oauth_disconnect_route_removed():
    """The pre-refactor OAuth disconnect route must no longer exist.

    Regression guard for the auth refactor: ``/apps/astrolabe/oauth/disconnect``
    (and the rest of the OAuth authorize/callback/disconnect surface) was
    removed in favour of session-minted JWTs.
    """
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.post(f"{ASTROLABE}/oauth/disconnect")
    assert resp.status_code == 404, (
        f"obsolete oauth/disconnect route still resolves ({resp.status_code})"
    )


async def test_settings_page_route_resolves():
    """The personal settings page route exists (auth-gated when no session)."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get(f"{NEXTCLOUD_URL}/settings/user/astrolabe")
    assert resp.status_code in ({200} | _UNAUTH), (
        f"unexpected status for settings page: {resp.status_code}"
    )
