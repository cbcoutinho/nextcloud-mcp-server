"""Integration test for issue #824 against the live login-flow MCP server.

`POST /api/v1/users/{user_id}/app-password` validates the supplied BasicAuth
credential against Nextcloud's OCS `/cloud/user` endpoint. Nextcloud keys
app-password auth on the *loginName*, which differs from the display name —
e.g. the admin account's display name is `Admin` (capital A) while its
loginName is `admin`, and a user "Test User" (with a space) has a distinct
loginName/UID.

When the supplied loginName does not authenticate, OCS v1 (`/ocs/v1.php`)
returns **HTTP 200** with `ocs.meta.statuscode: 997` and `ocs.data: []`. The
old handler gated auth failure on the HTTP status (`!= 200`), so it never
fired, fell through to `[].get("id")`, and raised
`AttributeError: 'list' object has no attribute 'get'` — escaping as an
unhandled **500**. The fix queries OCS v2 and parses defensively, so a failed
validation is a clean **401**.

These tests exercise the live `mcp-login-flow` container (port 8004), which
performs the OCS round-trip against the real Nextcloud. The credentials are
deliberately wrong, so validation fails for every UID shape under test —
capitals (`Admin`) and spaces (`Test User`) — which is exactly the path that
used to 500.
"""

import base64

import httpx
import pytest

LOGIN_FLOW_API_BASE_URL = "http://localhost:8004"

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]

# A syntactically valid app password (matches APP_PASSWORD_PATTERN) that is not
# a real credential for any account — so the OCS validation always fails. This
# is a throwaway test fixture, not a real secret.
_WRONG_APP_PASSWORD = "aaaaa-bbbbb-ccccc-ddddd-eeeee"  # NOSONAR(S2068)


def _basic_auth_header(username: str, password: str) -> str:
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {credentials}"


@pytest.mark.parametrize(
    "user_id",
    [
        pytest.param("Admin", id="capitalized-display-name"),
        pytest.param("Test User", id="display-name-with-space"),
    ],
)
async def test_provision_with_unresolvable_loginname_returns_401_not_500(user_id):
    """A loginName that fails OCS validation yields 401, never 500 (#824).

    Pre-fix, the capitalized/spaced loginName produced an OCS v1 ``200 +
    data: []`` payload that crashed parsing with an unhandled 500.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{LOGIN_FLOW_API_BASE_URL}/api/v1/users/{user_id}/app-password",
            headers={"Authorization": _basic_auth_header(user_id, _WRONG_APP_PASSWORD)},
        )

    assert response.status_code == 401, (
        f"provisioning for {user_id!r} returned {response.status_code} "
        f"(expected 401, not 500): {response.text}"
    )
    assert "Invalid app password" in response.json().get("error", "")


async def test_provision_with_mismatched_loginname_body_returns_401_not_500():
    """The body-supplied loginName is what's validated; a non-resolving
    loginName (display name with a space) still yields 401, not 500 (#824).

    Mirrors the production call shape where the UID in the path differs from the
    Nextcloud loginName sent in the JSON body.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{LOGIN_FLOW_API_BASE_URL}/api/v1/users/testuser/app-password",
            headers={
                "Authorization": _basic_auth_header("testuser", _WRONG_APP_PASSWORD)
            },
            json={"username": "Test User"},
        )

    assert response.status_code == 401, (
        f"provisioning returned {response.status_code} "
        f"(expected 401, not 500): {response.text}"
    )
    assert "Invalid app password" in response.json().get("error", "")
