"""Fixtures for Keycloak-service Login Flow v2 integration tests (port 8002).

The ``mcp-keycloak`` service uses Keycloak as the external OAuth IdP but reaches
Nextcloud via Login Flow v2 **app passwords** (``MCP_DEPLOYMENT_MODE=login_flow``),
exactly like the ``mcp-login-flow`` service — the only difference is the OAuth
IdP (Keycloak vs Nextcloud's built-in ``oidc`` app). The keycloak lane previously
only had DCR/authorize tests; it never provisioned a Login Flow v2 app password or
issued a real Nextcloud API call. These fixtures fill that gap end-to-end:

* **OAuth leg** — a Keycloak direct-grant (ROPC) obtains an access token that the
  ``mcp-keycloak`` session accepts. This exercises the keycloak service without
  driving Keycloak's browser login form.
* **Login Flow v2 leg** — the browser completes Nextcloud Login Flow v2 by logging
  in with the **email address** of the account that Keycloak token resolves to.
  Nextcloud keys the resulting app password on the *loginName* (the email), which
  differs from the user's canonical UID (loginName != UID).

The two legs must be the *same* Nextcloud account: since GHSA-84qv-22q6-x82r a
grant completed by anyone other than the OAuth caller is refused and never
stored. So the divergence comes from an email alias on the caller's own account,
not from a second user (which is how this lane originally staged it).

Getting these fixtures to provision at all is the regression guard for
``NEXTCLOUD_PUBLIC_URL``: in external-IdP mode the OAuth issuer URL is Keycloak,
so without a dedicated browser-reachable Nextcloud URL the Login Flow v2
``login_url`` is rewritten to Keycloak's origin and 404s.

Relation to PR #980: the login-by-email leg produces the same ``loginName != UID``
identity shape as #980's client fix, but it does NOT reproduce #980's wrong-path
failure on the CI Nextcloud versions — Nextcloud resolves
``/remote.php/dav/files/<email>/`` to the user's real home (email is a valid path
alias), so the WebDAV round-trip succeeds with or without the principal-discovery
fix. Neither can a plain Keycloak/``user_oidc`` login: ``user_oidc``'s
``LoginController`` hardcodes ``loginName == UID`` (the sha256 hash), so its DAV
paths are already correct. #980's failure mode needs a backend (e.g. LDAP) where
the loginName is not a valid files-path alias; it is covered by #980's own mocked
unit tests.
"""

import json
import logging
import os
import uuid
from typing import Any, AsyncGenerator

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.types import ElicitRequestParams, ElicitResult

from nextcloud_mcp_server.client import NextcloudClient
from tests.conftest import (
    create_mcp_client_session,
)
from tests.server.login_flow.conftest import _rewrite_login_flow_url

logger = logging.getLogger(__name__)

KEYCLOAK_MCP_URL = "http://localhost:8002/mcp"
KEYCLOAK_MCP_BASE_URL = "http://localhost:8002"
KEYCLOAK_BASE_URL = "http://localhost:8888"
KEYCLOAK_REALM = "nextcloud-mcp"

# Static confidential client from keycloak/realm-export.json. It permits the
# test callback (redirectUris include http://localhost:*) and carries audience
# mappers for both `nextcloud-mcp-server` (MCP validation) and `nextcloud`
# (user_oidc validation).
KEYCLOAK_CLIENT_ID = "nextcloud-mcp-server"
# Dev-only value mirrored from keycloak/realm-export.json + docker-compose.yml,
# not a real secret. NOSONAR suppresses the hardcoded-credentials hotspot.
KEYCLOAK_CLIENT_SECRET = "mcp-secret-change-in-production"  # NOSONAR(S2068)

# Keycloak user for the OAuth leg. Its identity is load-bearing since
# GHSA-84qv-22q6-x82r: the Nextcloud account it resolves to must be the one that
# completes the Login Flow leg, or the grant is refused (see
# `divergent_email_user`, which asserts exactly that). Direct Access Grants
# (ROPC) are enabled for the `nextcloud-mcp-server` client in realm-export.json.
KEYCLOAK_OAUTH_USER = "admin"
KEYCLOAK_OAUTH_PASSWORD = "admin"  # NOSONAR(S2068) - dev-only Keycloak bootstrap creds

# Scopes registered on the Keycloak `nextcloud-mcp-server` client
# (realm-export.json optionalClientScopes). This deliberately EXCLUDES
# ``talk.read``/``talk.write``: those are part of ``DEFAULT_FULL_SCOPES`` but are
# NOT registered on the Keycloak client, so requesting them makes Keycloak reject
# the whole token request with ``invalid_scope``. ``files.read``/``files.write``
# are what the WebDAV reproduction test actually needs.
KEYCLOAK_SUPPORTED_SCOPES = (
    "openid profile email "
    "notes.read notes.write "
    "calendar.read calendar.write "
    "todo.read todo.write "
    "contacts.read contacts.write "
    "cookbook.read cookbook.write "
    "deck.read deck.write "
    "tables.read tables.write "
    "files.read files.write "
    "sharing.read sharing.write"
)


async def _nextcloud_uid_for_bearer(token: str) -> str | None:
    """Canonical Nextcloud UID a Keycloak access token authenticates as.

    The same question ``auth/grant_ownership.py`` asks, and the reason it has to
    ask Nextcloud rather than read a claim: ``user_oidc`` is configured with
    ``--unique-uid``, so the UID is a hash of the IdP ``sub``.
    """
    host = os.getenv("NEXTCLOUD_HOST")
    assert host, "NEXTCLOUD_HOST must be set for the keycloak lane"
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(
            f"{host.rstrip('/')}/ocs/v2.php/cloud/user",
            headers={"Authorization": f"Bearer {token}", "OCS-APIRequest": "true"},
            params={"format": "json"},
        )
    response.raise_for_status()
    return response.json()["ocs"]["data"]["id"]


@pytest.fixture(scope="session")
async def divergent_email_user(
    anyio_backend,
    nc_client: NextcloudClient,
    keycloak_service_oauth_token: str,
) -> AsyncGenerator[dict[str, str], Any]:
    """The OAuth caller's own Nextcloud account, reachable under an email loginName.

    Yields a dict with ``uid``, ``email`` and ``password``. Nextcloud
    login-by-email is enabled by default, so logging in with the email during
    Login Flow v2 produces an app password whose stored loginName is the email —
    not the UID (loginName != UID, the identity shape this lane exists to
    exercise).

    The account is the *caller's own*, not a second user: since
    GHSA-84qv-22q6-x82r a Login Flow v2 grant completed by any account other than
    the OAuth caller is refused and never stored, so a separate local user — what
    this fixture used to create — can no longer provision at all. The divergence
    therefore comes from the email alias alone.

    Session-scoped: the ``mcp-keycloak`` app-password store is keyed by the
    Keycloak OAuth identity (a single shared ``admin``), so all tests share one
    provisioned app password and the email alias must stay in place for the whole
    session. It is restored on teardown.
    """
    uid = await _nextcloud_uid_for_bearer(keycloak_service_oauth_token)
    nc_username = os.getenv("NEXTCLOUD_USERNAME")
    nc_password = os.getenv("NEXTCLOUD_PASSWORD")
    assert nc_username and nc_password, (
        "NEXTCLOUD_USERNAME/NEXTCLOUD_PASSWORD must be set for the keycloak lane"
    )
    assert uid == nc_username, (
        f"Keycloak user {KEYCLOAK_OAUTH_USER!r} resolves to Nextcloud UID {uid!r}, "
        f"but this lane can only complete Login Flow v2 as {nc_username!r} (the "
        "only account whose password the tests know). Since GHSA-84qv-22q6-x82r "
        "the grant must come from the caller's own account, so the OAuth leg and "
        "the browser leg have to be the same user."
    )

    original_email = (await nc_client.users.get_user_details(uid)).email
    email = f"divprincipal_{uuid.uuid4().hex[:8]}@example.com"

    logger.info("Giving caller uid=%s the email alias %s", uid, email)
    await nc_client.users.update_user_field(uid, "email", email)

    try:
        yield {"uid": uid, "email": email, "password": nc_password}
    finally:
        try:
            await nc_client.users.update_user_field(uid, "email", original_email or "")
            logger.info("Restored email of %s", uid)
        except Exception as e:  # noqa: BLE001 - best-effort cleanup
            logger.warning("Failed to restore email of %s: %s", uid, e)


@pytest.fixture(scope="session")
async def keycloak_service_oauth_token(anyio_backend) -> str:
    """Obtain a Keycloak access token accepted by the ``mcp-keycloak`` session.

    Uses the OAuth 2.0 Resource Owner Password Credentials (direct access)
    grant against the static ``nextcloud-mcp-server`` client. Direct grant is
    faster than driving Keycloak's browser login form and avoids the flakiness
    of the auth-code + Playwright flow (whose native ``#username`` login page
    also breaks when the request carries scopes the client does not know about).

    *Which* user this authenticates as does matter, though — since
    GHSA-84qv-22q6-x82r the Nextcloud account this token resolves to has to be
    the one that completes the Login Flow leg, or the grant is refused. That is
    what ``divergent_email_user`` asserts before anything else runs.
    """
    async with httpx.AsyncClient(timeout=30.0) as http:
        discovery = await http.get(
            f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"
            "/.well-known/openid-configuration"
        )
        try:
            discovery.raise_for_status()
        except httpx.HTTPStatusError as e:
            pytest.skip(f"Keycloak realm not available: {e}")
        token_endpoint = discovery.json()["token_endpoint"]

        token_resp = await http.post(
            token_endpoint,
            data={
                "grant_type": "password",
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
                "username": KEYCLOAK_OAUTH_USER,
                "password": KEYCLOAK_OAUTH_PASSWORD,
                "scope": KEYCLOAK_SUPPORTED_SCOPES,
            },
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

    logger.info("Obtained Keycloak OAuth token (direct grant) for mcp-keycloak session")
    return access_token


async def _complete_login_flow_v2_with_email(
    browser, login_url: str, email: str, password: str
) -> None:
    """Complete Nextcloud Login Flow v2 logging in via EMAIL.

    Identical to the login_flow helper, but fills the Nextcloud login form's
    user field with the *email* address so the resulting app password's stored
    loginName is the email (not the UID) — the ``loginName != UID`` identity
    shape exercised by this lane.
    """
    login_url = _rewrite_login_flow_url(login_url)

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()
    try:
        logger.info("Opening Login Flow v2 URL: %s...", login_url[:80])
        await page.goto(login_url, wait_until="networkidle", timeout=60000)

        # Step 1: "Connect to your account" -> click "Log in" (exact match; the
        # connect page also renders "Alternative log in using app password").
        login_btn = page.get_by_role("button", name="Log in", exact=True)
        try:
            await login_btn.wait_for(timeout=10000)
            await login_btn.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            logger.info("No 'Log in' button - may already be on login/grant page")

        # Step 2: native login form -> fill EMAIL as the user identifier.
        user_field = page.locator('input[name="user"]')
        if await user_field.count() > 0:
            logger.info("Login form detected, logging in via email %s", email)
            await user_field.fill(email)
            await page.locator('input[name="password"]').fill(password)
            await page.get_by_role("button", name="Log in", exact=True).click()
            await page.wait_for_load_state("networkidle", timeout=60000)
        else:
            logger.info("No login form - already logged in via session")

        # Step 3: "Account access" grant page -> "Grant access".
        grant_btn = page.get_by_role("button", name="Grant access")
        try:
            await grant_btn.wait_for(timeout=15000)
            await grant_btn.click()
        except Exception as e:
            logger.warning("No Grant access button: %s", e)
            # Debug artifact uploaded by CI on failure (test.yml collects
            # /tmp/*.png). NOSONAR suppresses the world-writable-dir hotspot.
            shot_path = "/tmp/keycloak_login_flow_no_grant.png"  # NOSONAR(S5443)
            await page.screenshot(path=shot_path)

        # Step 4: password confirmation dialog.
        confirm_password = page.get_by_role("dialog").get_by_role(
            "textbox", name="Password"
        )
        try:
            await confirm_password.wait_for(timeout=10000)
            await confirm_password.fill(password)
            confirm_btn = page.get_by_role("dialog").get_by_role(
                "button", name="Confirm"
            )
            await confirm_btn.wait_for(timeout=5000)
            await confirm_btn.click()
        except Exception:
            logger.info(
                "No password confirmation dialog (may have been auto-confirmed)"
            )

        # Step 5: "Account connected" success page.
        try:
            await page.get_by_text("Account connected").wait_for(timeout=15000)
            logger.info("Login Flow v2 completed: Account connected!")
        except Exception:
            await page.wait_for_load_state("networkidle", timeout=10000)
            logger.info("Login Flow v2 done. Final URL: %s", page.url)
    finally:
        await context.close()


@pytest.fixture(scope="session")
async def nc_mcp_keycloak_email_client(
    anyio_backend,
    keycloak_service_oauth_token: str,
    browser,
    divergent_email_user: dict[str, str],
) -> AsyncGenerator[ClientSession, Any]:
    """Provisioned ``mcp-keycloak`` session whose app password loginName is an email.

    Session-scoped so a single Login Flow v2 provisioning (one browser login)
    serves every test in the keycloak lane. This is both faster and correct:
    the app password is keyed by the shared Keycloak ``admin`` identity, so
    re-provisioning per test would just hit ``already_provisioned`` and reuse the
    same stored password anyway (see ``divergent_email_user``).

    1. Connects to mcp-keycloak (8002) with a Keycloak OAuth token.
    2. Calls ``nc_auth_provision_access`` to start Login Flow v2.
    3. Completes the browser login as the caller's own account **via its email
       alias**, minting an app password whose loginName is the email.
    4. Polls ``nc_auth_check_status`` until provisioned, then yields the session.
    """
    email = divergent_email_user["email"]
    password = divergent_email_user["password"]
    login_url_holder: dict[str, str] = {}

    async def elicitation_callback(
        context: Any, params: ElicitRequestParams
    ) -> ElicitResult:
        for line in params.message.split("\n"):
            stripped = line.strip()
            if stripped.startswith("http") and "/login/v2/" in stripped:
                login_url_holder["url"] = stripped
                break
        if "url" in login_url_holder:
            await _complete_login_flow_v2_with_email(
                browser, login_url_holder["url"], email, password
            )
        return ElicitResult(action="accept", content={"acknowledged": True})

    async with create_mcp_client_session(
        url=KEYCLOAK_MCP_URL,
        token=keycloak_service_oauth_token,
        client_name="Keycloak MCP (email login)",
        elicitation_callback=elicitation_callback,
    ) as session:
        provision_result = await session.call_tool(
            "nc_auth_provision_access", {"scopes": None}
        )
        provision_data = json.loads(provision_result.content[0].text)
        logger.info("Provision status: %s", provision_data.get("status"))

        if provision_data.get("status") == "login_required":
            login_url = provision_data.get("login_url")
            if login_url and "url" not in login_url_holder:
                await _complete_login_flow_v2_with_email(
                    browser, login_url, email, password
                )

        for attempt in range(15):
            status_result = await session.call_tool("nc_auth_check_status", {})
            status_data = json.loads(status_result.content[0].text)
            status = status_data.get("status")
            logger.info("Status %s/15: %s", attempt + 1, status)
            if status == "provisioned":
                logger.info(
                    "Provisioned. Stored loginName=%s (expected email=%s)",
                    status_data.get("username"),
                    email,
                )
                break
            if status in ("not_initiated", "error"):
                raise RuntimeError(
                    f"Login Flow v2 failed: {status_data.get('message')}"
                )
            await anyio.sleep(2)
        else:
            raise TimeoutError("Login Flow v2 did not complete after 15 attempts")

        yield session
