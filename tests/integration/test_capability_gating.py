"""End-to-end checks for capability gating against a live Nextcloud.

What unit tests cannot prove is the payload shape: that a real instance really
advertises ``capabilities.<app>.version`` for the apps we gate on, and that a
real app id is distinguishable from a missing one. These tests assert exactly
that, plus that the module-level gates do not hide tools of installed apps from
the running MCP service.

Deliberately **non-mutating**: the single-user integration lane runs with
``-n 4 --dist loadfile``, so ``occ app:disable`` here would race the Cookbook,
Deck and Talk tests on other workers. The disable/re-enable round trip is a
documented manual check instead (docs/configuration.md → Capability-Gated
Tools).
"""

import logging

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.capabilities import clear_cache, unmet_capability
from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    clear_cache()
    yield
    clear_cache()


async def test_installed_app_satisfies_a_reachable_floor(nc_client: NextcloudClient):
    """A real instance advertises a parseable version for a gated app."""
    reason = await unmet_capability(nc_client, nc_client.username, "deck", "1.0.0")

    assert reason is None, reason


async def test_unreachable_floor_closes_the_gate(nc_client: NextcloudClient):
    """The advertised version is actually compared, not just present."""
    reason = await unmet_capability(nc_client, nc_client.username, "deck", "999.0.0")

    assert reason is not None
    assert "999.0.0" in reason


async def test_missing_app_closes_the_gate(nc_client: NextcloudClient):
    """An app the instance does not advertise is distinguishable from one it does."""
    reason = await unmet_capability(
        nc_client, nc_client.username, "not_a_real_nextcloud_app", None
    )

    assert reason is not None
    assert "not_a_real_nextcloud_app" in reason


async def test_gated_tools_of_installed_apps_stay_listed(nc_mcp_client: ClientSession):
    """The presence gates must not hide tools of apps the dev stack enables."""
    names = {tool.name for tool in (await nc_mcp_client.list_tools()).tools}

    # Gated via APP_CAPABILITY_KEY — all enabled by the post-installation hooks.
    assert "deck_get_boards" in names
    assert "nc_notes_search_notes" in names
    assert "nc_tables_list_tables" in names
    assert "nc_cookbook_list_recipes" in names
    # Never gated: these speak CalDAV/CardDAV and work without the web apps.
    assert "nc_calendar_list_calendars" in names
    assert "nc_contacts_list_addressbooks" in names
