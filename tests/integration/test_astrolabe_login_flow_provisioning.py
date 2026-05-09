"""Integration test for Astrolabe's "Enable Semantic Search" OAuth flow on
the `mcp-login-flow` profile.

Cross-system interface test. Brings together Astrolabe (Nextcloud PHP app
installed at container start by ``app-hooks/post-installation``) with the
``mcp-login-flow`` MCP server over OAuth + the management API. Mirrors
the production-shaped flow that PR #773's recent
`ALLOWED_MGMT_CLIENT` ↔ `astrolabeMcpClientOAuth00000000000` drift was
masking — every management API call from Astrolabe (e.g.
``/api/v1/users/admin/session``) was returning 401 because the
real-deployment client id was not in the test-fixture allowlist, so the
Astrolabe settings page never updated to reflect a successful
authorization.

This test is **regression coverage** for that class of drift. If the
Astrolabe client id ever falls out of `mcp-login-flow`'s
``ALLOWED_MGMT_CLIENT`` again, the post-redirect assertions here will
fail because the page state stays on ``oauth-required.php``.

Requires the login-flow stack to be running:

    MCP_SERVER_URL=http://mcp-login-flow:8004 \\
        docker compose --profile login-flow up -d app db mcp-login-flow

The ``app-hooks/before-starting/26-configure-astrolabe-oauth.sh`` hook
creates the OAuth client with the production-shaped id
``astrolabeMcpClientOAuth00000000000`` automatically when
``MCP_SERVER_URL`` is set, so no fixture-level OIDC client creation is
needed here.
"""

import logging
import os
import re

import pytest
from playwright.async_api import Page

# Reuse helpers from the multi-user-basic Astrolabe test for login + nav.
from tests.integration.test_astrolabe_multi_user_background_sync import (
    login_to_nextcloud,
    navigate_to_astrolabe_settings,
)

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]

NEXTCLOUD_URL = "http://localhost:8080"
ASTROLABE_SETTINGS_URL = f"{NEXTCLOUD_URL}/settings/user/astrolabe"


async def _click_enable_semantic_search(page: Page) -> None:
    """Click the "Enable Semantic Search" OAuth link on the
    ``oauth-required.php`` template that login-flow mode renders to a
    not-yet-authorized user.

    Astrolabe's own e2e helper (``third_party/astrolabe/tests/e2e/helpers/
    authorize.ts``) targets the same link by accessible name.
    """
    enable_link = page.get_by_role("link", name="Enable Semantic Search")
    await enable_link.wait_for(state="visible", timeout=10_000)
    logger.info("Clicking 'Enable Semantic Search' OAuth link")
    await enable_link.click()


async def _grant_oidc_consent(page: Page) -> None:
    """Click "Allow" on the Nextcloud OIDC consent screen, if shown.

    Nextcloud may auto-redirect for already-trusted clients, in which
    case the consent button never appears — that's not an error.
    """
    allow_button = page.get_by_role("button", name=re.compile(r"^allow$", re.I))
    try:
        await allow_button.wait_for(state="visible", timeout=10_000)
        logger.info("Clicking 'Allow' on OIDC consent")
        await allow_button.click(force=True)
    except Exception:
        logger.info(
            "OIDC consent screen not visible — assuming auto-grant for "
            "already-trusted client"
        )


@pytest.mark.timeout(180)
async def test_enable_semantic_search_completes_oauth_for_login_flow(browser):
    """Click the "Enable Semantic Search" link, grant consent, and assert
    the post-redirect page reflects a completed authorization.

    The success criterion is intentionally negative: after the OAuth
    flow, the original "Enable Semantic Search" link must be gone. If
    Astrolabe's management API call is rejected by the MCP server (HTTP
    401, the original bug), the page falls back to the same
    ``oauth-required.php`` template and the link reappears — making this
    test the canary for the drift class.
    """
    admin_password = os.getenv("NEXTCLOUD_PASSWORD")
    if admin_password is None:
        raise RuntimeError("NEXTCLOUD_PASSWORD must be set")
    page = await browser.new_page()
    try:
        await login_to_nextcloud(page, "admin", admin_password)
        await navigate_to_astrolabe_settings(page)

        # Sanity-check we're on the not-yet-authorized template.
        enable_link = page.get_by_role("link", name="Enable Semantic Search")
        if await enable_link.count() == 0:
            pytest.skip(
                "Astrolabe is already authorized for admin (oauth-required.php "
                "not rendered). Reset by clearing the user's OAuth tokens "
                "before re-running this test."
            )

        await _click_enable_semantic_search(page)
        await _grant_oidc_consent(page)

        # OAuth callback returns to /apps/astrolabe/oauth/callback then the
        # controller redirects to /settings/user/astrolabe.
        await page.wait_for_url(re.compile(r"/settings/user/astrolabe"), timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)

        # Regression assertion for the ALLOWED_MGMT_CLIENT drift bug:
        # the page must have moved past oauth-required.php. If
        # Astrolabe's management API call to /api/v1/users/{id}/session
        # is rejected (401), the session lookup falls back to "no token",
        # and the same oauth-required.php template re-renders with the
        # link still present.
        post_auth_count = await page.get_by_role(
            "link", name="Enable Semantic Search"
        ).count()
        assert post_auth_count == 0, (
            "'Enable Semantic Search' link still visible after completing "
            "OAuth flow — Astrolabe could not read the user's session from "
            "the MCP server. Most likely cause: "
            "`astrolabeMcpClientOAuth00000000000` missing from "
            "`ALLOWED_MGMT_CLIENT` on `mcp-login-flow`."
        )
    finally:
        await page.close()
