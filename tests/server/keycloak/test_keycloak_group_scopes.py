"""
Per-group scope limits with Keycloak as the IdP (Deck card #1329).

Keycloak needs no extension for this: a client scope with realm-role scope
mappings is only granted to users holding one of those roles, and groups grant
roles. keycloak/realm-export.json maps ``notes.write`` → role ``notes-writer``
(group ``/notes-writers``) and ``files.write`` → ``files-writer``
(``/files-writers``):

- ``admin`` is in both groups (the rest of the lane keeps full scopes),
- ``test_write_only`` is in ``/files-writers`` only,
- ``test_read_only`` is in neither.

Keycloak drops a scope the user is not permitted silently rather than failing
the request, so every user requests the full supported set.
"""

import base64
import json

import httpx
import pytest

from .conftest import (
    KEYCLOAK_BASE_URL,
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_CLIENT_SECRET,
    KEYCLOAK_OAUTH_PASSWORD,
    KEYCLOAK_OAUTH_USER,
    KEYCLOAK_REALM,
    KEYCLOAK_SUPPORTED_SCOPES,
)

pytestmark = [pytest.mark.integration, pytest.mark.keycloak]

# realm-export.json fixture users; dev-only, not a real secret.
TEST_USER_PASSWORD = "test123"  # NOSONAR(S2068)

TOKEN_ENDPOINT = (
    f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
)


def _scope_claim(token: str) -> set[str]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return set(json.loads(base64.urlsafe_b64decode(payload))["scope"].split())


async def _granted_scopes(username: str, password: str) -> set[str]:
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "password",
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
                "username": username,
                "password": password,
                "scope": KEYCLOAK_SUPPORTED_SCOPES,
            },
        )
    if response.status_code == 404:
        pytest.skip("Keycloak realm not available")
    response.raise_for_status()
    return _scope_claim(response.json()["access_token"])


@pytest.mark.parametrize(
    ("username", "password", "granted", "withheld"),
    [
        (
            KEYCLOAK_OAUTH_USER,
            KEYCLOAK_OAUTH_PASSWORD,
            {"notes.write", "files.write"},
            set(),
        ),
        ("test_write_only", TEST_USER_PASSWORD, {"files.write"}, {"notes.write"}),
        ("test_read_only", TEST_USER_PASSWORD, set(), {"notes.write", "files.write"}),
    ],
)
async def test_group_limits_write_scopes(
    anyio_backend, username, password, granted, withheld
):
    scopes = await _granted_scopes(username, password)

    # Ungated scopes are unaffected by group membership.
    assert {"notes.read", "files.read"} <= scopes, scopes
    assert granted <= scopes, scopes
    assert not (withheld & scopes), scopes
