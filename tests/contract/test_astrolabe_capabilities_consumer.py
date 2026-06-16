"""Consumer contract: nextcloud-mcp-server -> astrolabe OCS capabilities.

The MCP server reads which content sources an admin has approved for semantic
search via :func:`nextcloud_mcp_server.capabilities.allowed_doc_types`, which
calls :meth:`NextcloudClient.capabilities` →
``GET /ocs/v2.php/cloud/capabilities`` and parses
``ocs.data.capabilities.astrolabe.semantic_search.enabled_doc_types`` (the
``OCA\\Astrolabe\\Capabilities`` provider on the astrolabe side).

This pact pins the request shape and the two states the consumer branches on:

- some sources approved -> ``enabled_doc_types`` is a non-empty list, parsed to
  the corresponding frozenset (the search/scan/purge gates restrict to it)
- every source disabled  -> ``enabled_doc_types`` is ``[]``, parsed to an empty
  frozenset (an all-disabled admin config — distinct from "no restriction")

The "no astrolabe block" / fail-open (``None``) case is internal defensive
handling for older astrolabe versions, not a contract obligation of the current
provider, so it is covered by a unit test (``tests/unit/test_capabilities.py``)
rather than a pact interaction.

Only the capability block astrolabe owns is pinned — the real OCS response also
carries many other apps' capabilities, which pact ignores (unspecified keys are
allowed in the provider response).

See ADR-029 for the overall contract-testing architecture.
"""

import pytest
from httpx import BasicAuth

from nextcloud_mcp_server.capabilities import allowed_doc_types, clear_cache
from nextcloud_mcp_server.client import NextcloudClient

pytestmark = pytest.mark.contract


def _ocs_capabilities(enabled_doc_types: list[str]) -> dict:
    """Minimal OCS envelope carrying just astrolabe's semantic_search block."""
    return {
        "ocs": {
            "meta": {"status": "ok"},
            "data": {
                "capabilities": {
                    "astrolabe": {
                        "semantic_search": {
                            "enabled_doc_types": enabled_doc_types,
                        }
                    }
                }
            },
        }
    }


async def test_capabilities_report_admin_approved_doc_types(consumer_pact):
    """Approved sources are returned and parsed into the allow-set."""
    clear_cache()
    (
        consumer_pact.upon_receiving("a request for OCS capabilities")
        .given("astrolabe has approved file and note for semantic search")
        .with_request("GET", "/ocs/v2.php/cloud/capabilities")
        .with_header("OCS-APIRequest", "true")
        .will_respond_with(200)
        .with_body(
            _ocs_capabilities(["file", "note"]),
            content_type="application/json",
        )
    )

    with consumer_pact.serve() as srv:
        client = NextcloudClient(
            base_url=str(srv.url),
            username="admin",
            auth=BasicAuth("admin", "app-password"),
        )
        allowed = await allowed_doc_types(client, "admin")

    assert allowed == frozenset({"file", "note"})


async def test_capabilities_report_all_sources_disabled(consumer_pact):
    """An empty allow-set (admin disabled everything) is distinct from None."""
    clear_cache()
    (
        consumer_pact.upon_receiving(
            "a request for OCS capabilities with every source disabled"
        )
        .given("astrolabe has disabled all sources for semantic search")
        .with_request("GET", "/ocs/v2.php/cloud/capabilities")
        .with_header("OCS-APIRequest", "true")
        .will_respond_with(200)
        .with_body(
            _ocs_capabilities([]),
            content_type="application/json",
        )
    )

    with consumer_pact.serve() as srv:
        client = NextcloudClient(
            base_url=str(srv.url),
            username="admin",
            auth=BasicAuth("admin", "app-password"),
        )
        allowed = await allowed_doc_types(client, "admin")

    # Present-but-empty => empty frozenset (restrict everything), NOT None.
    assert allowed == frozenset()
