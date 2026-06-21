"""Consumer contract: nextcloud-mcp-server -> astrolabe OCS capabilities.

The MCP server reads which content sources an admin has approved for semantic
search via :func:`nextcloud_mcp_server.capabilities.allowed_doc_types`, which
calls :meth:`NextcloudClient.capabilities` →
``GET /ocs/v2.php/cloud/capabilities`` and parses
``ocs.data.capabilities.astrolabe.semantic_search.enabled_doc_types`` (the
``OCA\\Astrolabe\\Capabilities`` provider on the astrolabe side).

This pact pins the request shape and the states the consumer branches on:

- some sources approved -> ``enabled_doc_types`` is a non-empty list, parsed to
  the corresponding frozenset (the search/scan/purge gates restrict to it)
- every source disabled  -> ``enabled_doc_types`` is ``[]``, parsed to an empty
  frozenset (an all-disabled admin config — distinct from "no restriction")
- per-user narrowing -> the capability call is authenticated per-user, so
  ``enabled_doc_types`` is the *effective* set for the requesting user
  (admin-enabled ∩ that user's own choices). The MCP server consumes the same
  field unchanged, so a user who disabled a source sees it absent from their
  set. This pins that per-user contract so astrolabe can't regress it.

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
    """Minimal OCS envelope carrying just astrolabe's semantic_search block.

    Intentionally omits the rest of a real OCS response (other apps'
    capabilities, ``meta.statuscode``/``message``, etc.): Pact V4 allows extra
    provider-side keys, so pinning only the block this consumer reads keeps the
    contract focused on what astrolabe owns without coupling to Nextcloud-core
    envelope fields.
    """
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


async def test_capabilities_reflect_per_user_narrowing(consumer_pact):
    """enabled_doc_types is the requesting user's effective set (admin ∩ user).

    The capability call is authenticated per-user, so a user who disabled
    ``notes`` for themselves sees ``note`` absent even though the admin allows
    it. The MCP server reads the same field, so per-user gating/deletion needs
    no wire-shape change.
    """
    clear_cache()
    (
        consumer_pact.upon_receiving(
            "a request for OCS capabilities by a user who disabled notes"
        )
        .given("astrolabe reports effective sources for a user who disabled notes")
        .with_request("GET", "/ocs/v2.php/cloud/capabilities")
        .with_header("OCS-APIRequest", "true")
        .will_respond_with(200)
        .with_body(
            # Admin allows file+note tenant-wide; this user narrowed out note,
            # so their effective set is just file.
            _ocs_capabilities(["file"]),
            content_type="application/json",
        )
    )

    with consumer_pact.serve() as srv:
        client = NextcloudClient(
            base_url=str(srv.url),
            username="bob",
            auth=BasicAuth("bob", "app-password"),
        )
        allowed = await allowed_doc_types(client, "bob")

    assert allowed == frozenset({"file"})
