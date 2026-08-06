"""Server-layer regression: ``nc_contacts_list_addressbooks`` must not crash
on an addressbook with a null display name.

Nextcloud sends an empty ``<d:displayname/>`` when none is configured; the
client now falls back to the URI slug, and the tool's model mapping must
defend the same boundary (a ``None`` value, not a missing key, is what
``dict.get`` with a default does not cover).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from nextcloud_mcp_server.server.contacts import configure_contacts_tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path.

    Same rationale as the WebDAV tool tests: these invoke the tool function
    directly with no transport, so under any OAuth-style mode the decorator
    would deny the call.
    """
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def contacts_tools() -> dict:
    """Register the contacts tools on a fresh FastMCP and return them by name."""
    mcp = FastMCP(name="test-contacts-tools")
    configure_contacts_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


def _mock_ctx(client) -> SimpleNamespace:
    """Minimal Context-shaped object for the tool decorators."""
    ctx = SimpleNamespace()
    ctx.request_context = SimpleNamespace()
    ctx._client = client
    return ctx


@pytest.fixture
def patch_get_client(mocker):
    """Replace ``get_client`` in the contacts server module with a mock."""

    def _install(client):
        async def fake_get_client(ctx):
            return client

        mocker.patch(
            "nextcloud_mcp_server.server.contacts.get_client",
            side_effect=fake_get_client,
        )

    return _install


async def test_list_addressbooks_null_displayname_falls_back_to_slug(
    contacts_tools, patch_get_client
):
    client = SimpleNamespace()
    client.contacts = AsyncMock()
    client.contacts.list_addressbooks = AsyncMock(
        return_value=[
            {"name": "contacts", "display_name": None, "getctag": None},
            {"name": "family", "display_name": "Family", "getctag": '"x"'},
        ]
    )
    patch_get_client(client)

    result = await contacts_tools["nc_contacts_list_addressbooks"].fn(
        ctx=_mock_ctx(client)
    )

    assert result.total_count == 2
    by_uri = {ab.uri: ab for ab in result.addressbooks}
    assert by_uri["contacts"].displayname == "contacts"
    assert by_uri["family"].displayname == "Family"


async def test_list_addressbooks_missing_displayname_key_falls_back_to_slug(
    contacts_tools, patch_get_client
):
    """A client payload without a ``display_name`` key behaves the same."""
    client = SimpleNamespace()
    client.contacts = AsyncMock()
    client.contacts.list_addressbooks = AsyncMock(
        return_value=[{"name": "work", "getctag": None}]
    )
    patch_get_client(client)

    result = await contacts_tools["nc_contacts_list_addressbooks"].fn(
        ctx=_mock_ctx(client)
    )

    assert result.total_count == 1
    assert result.addressbooks[0].uri == "work"
    assert result.addressbooks[0].displayname == "work"
