"""Contract: astrolabe UI -> nextcloud-mcp-server ``GET /api/v1/status``.

For the status endpoint the roles are reversed from the other contract tests in
this package: **astrolabe is the consumer** and **nextcloud-mcp-server is the
provider**. The astrolabe UI reads ``supported_search_types`` from
``/api/v1/status`` to gate which query types it offers (ADR-030):

- ``SEARCH_MODE=hybrid`` → ``["semantic", "bm25", "hybrid"]``
- ``SEARCH_MODE=keyword`` → ``["bm25"]`` (dense embeddings are off)
- vector sync disabled  → ``[]``

This is **contract-first**: we author the pact astrolabe's follow-up UI PR will
implement against, pinning the field name, the value vocabulary, and the
provider-state strings. The matching server-side states are registered in
``test_mcp_provider_verification.py`` so the provider verification job honours
them once astrolabe publishes its real consumer pact.

The generated pact is written to ``provider_contracts/`` (NOT ``pacts/``) so the
``pact-broker publish tests/contract/pacts`` step never publishes it under *our*
consumer identity — astrolabe owns and publishes the real consumer pact. The
real provider response is verified by ``tests/unit/test_management_status_endpoint.py``
(``TestStatusEndpointSearchTypes``) and, in integration CI, by provider
verification against a running server.

See ADR-029 (contract architecture) and ADR-030 (SEARCH_MODE).
"""

import shutil
from pathlib import Path

import httpx
import pytest
from pact import Pact

pytestmark = pytest.mark.contract

# Roles are the inverse of conftest's consumer_pact fixture (which is
# consumer=nextcloud-mcp-server, provider=astrolabe), so build the Pact directly.
CONSUMER = "astrolabe"
PROVIDER = "nextcloud-mcp-server"

# Deliberately NOT tests/contract/pacts/: that directory is published to the
# broker as *our* (mcp-as-consumer) pacts. astrolabe owns this consumer pact, so
# keep our contract-first copy out of the published set.
PROVIDER_CONTRACT_DIR = Path(__file__).parent / "provider_contracts"


@pytest.fixture(scope="module", autouse=True)
def _clean_provider_contract_dir():
    """Start the module from an empty provider_contracts directory."""
    if PROVIDER_CONTRACT_DIR.exists():
        shutil.rmtree(PROVIDER_CONTRACT_DIR)
    PROVIDER_CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
def status_pact():
    """A fresh Pact (consumer=astrolabe, provider=nextcloud-mcp-server)."""
    pact = Pact(CONSUMER, PROVIDER).with_specification("V4")
    yield pact
    pact.write_file(PROVIDER_CONTRACT_DIR, overwrite=False)


async def _fetch_supported_search_types(base_url: str) -> list[str]:
    """Stand-in for astrolabe's UI client: read the advertised query types.

    Mirrors what the astrolabe McpServerClient/UI does — GET the public status
    endpoint and read the ``supported_search_types`` array to populate its
    query-type picker.
    """
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.get("/api/v1/status")
        resp.raise_for_status()
        return resp.json()["supported_search_types"]


async def test_status_advertises_all_query_types_in_hybrid_mode(status_pact):
    """Hybrid mode advertises semantic + bm25 + hybrid, so the UI offers all."""
    (
        status_pact.upon_receiving("a status request when the server is in hybrid mode")
        .given("the server advertises hybrid search support")
        .with_request("GET", "/api/v1/status")
        .will_respond_with(200)
        .with_body(
            # Pin only the field astrolabe reads; Pact V4 allows the real
            # response to carry the other status fields (version, auth_mode, …).
            {"supported_search_types": ["semantic", "bm25", "hybrid"]},
            content_type="application/json",
        )
    )

    with status_pact.serve() as srv:
        types = await _fetch_supported_search_types(str(srv.url))

    assert types == ["semantic", "bm25", "hybrid"]


async def test_status_advertises_bm25_only_in_keyword_mode(status_pact):
    """Keyword mode advertises bm25 only, so the UI hides semantic/hybrid."""
    (
        status_pact.upon_receiving(
            "a status request when the server is in keyword mode"
        )
        .given("the server advertises keyword-only search support")
        .with_request("GET", "/api/v1/status")
        .will_respond_with(200)
        .with_body(
            {"supported_search_types": ["bm25"]},
            content_type="application/json",
        )
    )

    with status_pact.serve() as srv:
        types = await _fetch_supported_search_types(str(srv.url))

    assert types == ["bm25"]
