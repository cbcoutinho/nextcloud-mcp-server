"""Consumer contract: nextcloud-mcp-server -> astrolabe credentials status API.

The MCP server checks a user's background-sync provisioning status via
:meth:`AstrolabeClient.get_background_sync_status`, which calls astrolabe's
admin credentials-metadata endpoint
(``GET /apps/astrolabe/api/v1/background-sync/credentials/{user_id}``). That
endpoint returns **presence/timestamps only — never the app password itself**
(the password reaches the MCP server out-of-band, pushed by astrolabe to
``POST /api/v1/users/{user_id}/app-password``).

This pact pins the request shape and the two states the consumer branches on:

- provisioned   -> ``has_background_access: true``,  ``sync_type: "app_password"``
- unprovisioned -> ``has_background_access: false``, ``sync_type: null``

The OAuth token fetch (:meth:`AstrolabeClient.get_access_token`) is stubbed so
only the status call hits the Pact mock server.

See ADR-029 for the overall contract-testing architecture.
"""

import pytest
from pact import match

from nextcloud_mcp_server.auth.astrolabe_client import AstrolabeClient

pytestmark = pytest.mark.contract

# Matches the ``Authorization: Bearer <token>`` header the client always sends.
_BEARER = match.regex("Bearer test-token", regex=r"Bearer .+")


async def test_status_reports_access_for_provisioned_user(consumer_pact, mocker):
    """A provisioned user is reported as having background access."""
    (
        consumer_pact.upon_receiving(
            "a request for a provisioned user's background-sync status"
        )
        .given("user alice has provisioned background-sync credentials")
        .with_request("GET", "/apps/astrolabe/api/v1/background-sync/credentials/alice")
        .with_header("Authorization", _BEARER)
        .will_respond_with(200)
        .with_body(
            {
                "success": True,
                "user_id": "alice",
                "has_background_access": True,
                "sync_type": "app_password",
                # Unix seconds (astrolabe BackgroundSyncCredentialStorage::getProvisionedAt),
                # not an ISO string.
                "provisioned_at": match.integer(1717000000),
            },
            content_type="application/json",
        )
    )

    with consumer_pact.serve() as srv:
        client = AstrolabeClient(
            nextcloud_host=str(srv.url), client_id="mcp", client_secret="secret"
        )
        mocker.patch.object(client, "get_access_token", return_value="test-token")

        status = await client.get_background_sync_status("alice")

    assert status["has_access"] is True
    assert status["credential_type"] == "app_password"
    assert status["provisioned_at"] == 1717000000


async def test_status_reports_no_access_for_unprovisioned_user(consumer_pact, mocker):
    """An unprovisioned user is reported as having no background access."""
    (
        consumer_pact.upon_receiving(
            "a request for an unprovisioned user's background-sync status"
        )
        .given("user bob has no background-sync credentials")
        .with_request("GET", "/apps/astrolabe/api/v1/background-sync/credentials/bob")
        .with_header("Authorization", _BEARER)
        .will_respond_with(200)
        .with_body(
            {
                "success": True,
                "user_id": "bob",
                "has_background_access": False,
                "sync_type": None,
                "provisioned_at": None,
            },
            content_type="application/json",
        )
    )

    with consumer_pact.serve() as srv:
        client = AstrolabeClient(
            nextcloud_host=str(srv.url), client_id="mcp", client_secret="secret"
        )
        mocker.patch.object(client, "get_access_token", return_value="test-token")

        status = await client.get_background_sync_status("bob")

    assert status["has_access"] is False
    assert status["credential_type"] is None
    assert status["provisioned_at"] is None
