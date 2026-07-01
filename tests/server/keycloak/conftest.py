"""Fixtures for Keycloak-service Login Flow v2 integration tests (port 8002).

The ``mcp-keycloak`` service uses Keycloak as the external OAuth IdP but reaches
Nextcloud via Login Flow v2 **app passwords** (``MCP_DEPLOYMENT_MODE=login_flow``),
exactly like the ``mcp-login-flow`` service — the only difference is the OAuth
IdP (Keycloak vs Nextcloud's built-in ``oidc`` app). The keycloak lane currently
only has DCR/authorize tests; the Login Flow v2 app-password integration tests
are missing.

These fixtures fill that gap AND set up the divergent-principal condition fixed
by PR #980:

* **OAuth leg** — a Keycloak auth-code flow (browser) obtains an access token
  that the ``mcp-keycloak`` session accepts. This exercises the keycloak service.
* **Login Flow v2 leg** — the browser completes Nextcloud Login Flow v2 by logging
  in as a *local* Nextcloud user using its **email address**. Nextcloud keys the
  resulting app password on the *loginName* (the email), which differs from the
  user's canonical UID. ``context.py`` then builds DAV paths from the email
  (``/remote.php/dav/files/<email>/``) instead of the UID — the exact wrong-path
  bug PR #980 fixes via ``current-user-principal`` discovery.

A plain Keycloak/user_oidc login can NOT reproduce this: ``user_oidc``'s
``LoginController`` sets ``loginName == UID`` (the sha256 hash), so its DAV paths
are already correct. Login-by-email of a local user is the reliable divergence
generator (and matches PR #980's own ``alice@example.com`` unit tests).
"""

import base64
import hashlib
import json
import logging
import secrets
import time
import uuid
from typing import Any, AsyncGenerator
from urllib.parse import quote

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.types import ElicitRequestParams, ElicitResult

from nextcloud_mcp_server.client import NextcloudClient
from tests.conftest import (
    DEFAULT_FULL_SCOPES,
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
KEYCLOAK_CLIENT_SECRET = "mcp-secret-change-in-production"

# Keycloak user used only for the OAuth leg (session identity key). It does not
# have to match the Nextcloud data user — the app password minted by the Login
# Flow leg is what authenticates DAV requests.
KEYCLOAK_OAUTH_USER = "admin"
KEYCLOAK_OAUTH_PASSWORD = "admin"


@pytest.fixture()
async def divergent_email_user(
    anyio_backend, nc_client: NextcloudClient
) -> AsyncGenerator[dict[str, str], Any]:
    """Create a local Nextcloud user whose loginName (email) differs from its UID.

    Yields a dict with ``uid``, ``email``, ``password`` and ``display_name``.
    The user is deleted on teardown. Nextcloud login-by-email is enabled by
    default, so logging in with the email during Login Flow v2 produces an app
    password whose stored loginName is the email — not the UID.
    """
    suffix = uuid.uuid4().hex[:8]
    uid = f"divprincipal_{suffix}"
    user = {
        "uid": uid,
        "email": f"{uid}@example.com",
        "password": "DivergentPrincipalPass123!",
        "display_name": f"Divergent Principal {suffix}",
    }

    logger.info("Creating divergent-principal user uid=%s email=%s", uid, user["email"])
    await nc_client.users.create_user(
        userid=uid,
        password=user["password"],
        display_name=user["display_name"],
        email=user["email"],
    )

    try:
        yield user
    finally:
        try:
            await nc_client.users.delete_user(uid)
            logger.info("Deleted divergent-principal user %s", uid)
        except Exception as e:  # noqa: BLE001 - best-effort cleanup
            logger.warning("Failed to delete divergent-principal user %s: %s", uid, e)


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for a PKCE S256 exchange."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@pytest.fixture()
async def keycloak_service_oauth_token(
    anyio_backend, browser, oauth_callback_server
) -> str:
    """Obtain a Keycloak access token accepted by the ``mcp-keycloak`` session.

    Drives the OAuth auth-code flow (with PKCE) against Keycloak using the
    static ``nextcloud-mcp-server`` client and the test OAuth callback server.
    Logs into Keycloak (not Nextcloud) via its native login form.
    """
    auth_states, callback_url = oauth_callback_server

    async with httpx.AsyncClient(timeout=30.0) as http:
        discovery = await http.get(
            f"{KEYCLOAK_BASE_URL}/realms/{KEYCLOAK_REALM}"
            "/.well-known/openid-configuration"
        )
        try:
            discovery.raise_for_status()
        except httpx.HTTPStatusError as e:
            pytest.skip(f"Keycloak realm not available: {e}")
        oidc = discovery.json()

    authorization_endpoint = oidc["authorization_endpoint"]
    token_endpoint = oidc["token_endpoint"]

    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    auth_url = (
        f"{authorization_endpoint}?"
        f"response_type=code&"
        f"client_id={KEYCLOAK_CLIENT_ID}&"
        f"redirect_uri={quote(callback_url, safe='')}&"
        f"state={state}&"
        f"scope={quote(DEFAULT_FULL_SCOPES, safe='')}&"
        f"code_challenge={challenge}&"
        f"code_challenge_method=S256"
    )

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()
    try:
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)

        # Keycloak login form (native). Field ids are stable across KC 26.x.
        await page.wait_for_selector("#username", timeout=15000)
        await page.fill("#username", KEYCLOAK_OAUTH_USER)
        await page.fill("#password", KEYCLOAK_OAUTH_PASSWORD)
        await page.click("#kc-login")
        await page.wait_for_load_state("networkidle", timeout=60000)

        start = time.time()
        while state not in auth_states:
            if time.time() - start > 45:
                await page.screenshot(path="/tmp/keycloak_oauth_timeout.png")
                raise TimeoutError(
                    f"Timeout waiting for Keycloak OAuth callback (url={page.url})"
                )
            await anyio.sleep(0.5)
        auth_code = auth_states[state]
    finally:
        await context.close()

    async with httpx.AsyncClient(timeout=30.0) as http:
        token_resp = await http.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": callback_url,
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
                "code_verifier": verifier,
            },
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

    logger.info("Obtained Keycloak OAuth token for mcp-keycloak session")
    return access_token


async def _complete_login_flow_v2_with_email(
    browser, login_url: str, email: str, password: str
) -> None:
    """Complete Nextcloud Login Flow v2 logging in as a local user via EMAIL.

    Identical to the login_flow helper, but fills the Nextcloud login form's
    user field with the *email* address so the resulting app password's stored
    loginName is the email (not the UID). This is what creates the divergent
    principal path that PR #980 fixes.
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
            await page.screenshot(path="/tmp/keycloak_login_flow_no_grant.png")

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


@pytest.fixture()
async def nc_mcp_keycloak_email_client(
    anyio_backend,
    keycloak_service_oauth_token: str,
    browser,
    divergent_email_user: dict[str, str],
) -> AsyncGenerator[ClientSession, Any]:
    """Provisioned ``mcp-keycloak`` session whose app password loginName is an email.

    1. Connects to mcp-keycloak (8002) with a Keycloak OAuth token.
    2. Calls ``nc_auth_provision_access`` to start Login Flow v2.
    3. Completes the browser login as the local ``divergent_email_user`` **via
       its email**, minting an app password whose loginName is the email.
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
