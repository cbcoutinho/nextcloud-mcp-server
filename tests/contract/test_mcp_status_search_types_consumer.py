"""Contract: astrolabe UI -> nextcloud-mcp-server ``GET /api/v1/status``.

For the status endpoint the roles are reversed from the other contract tests in
this package: **astrolabe is the consumer** and **nextcloud-mcp-server is the
provider**. The astrolabe UI reads ``supported_search_types`` from
``/api/v1/status`` to gate which query types it offers (ADR-031):

- vector sync enabled  → ``["semantic", "bm25", "hybrid"]`` (the collection is
  always dense-capable; keyword-only documents contribute via the sparse side)
- vector sync disabled → ``[]``

This is **contract-first**: we author the pact astrolabe's follow-up UI PR will
implement against, pinning the field name, the value vocabulary, and the
provider-state strings. The matching server-side states are registered in
``test_mcp_provider_verification.py`` so the provider verification job honours
them once astrolabe publishes its real consumer pact.

The generated pact is written to ``provider_contracts/`` (NOT ``pacts/``) so the
``pact broker publish tests/contract/pacts`` step never publishes it under *our*
consumer identity — astrolabe owns and publishes the real consumer pact. The
real provider response is verified by ``tests/unit/test_management_status_endpoint.py``
(``TestStatusEndpointSearchTypes``) and, in integration CI, by provider
verification against a running server.

See ADR-029 (contract architecture) and ADR-031 (per-document index mode).
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


async def test_status_advertises_all_query_types_when_vector_sync_enabled(status_pact):
    """Vector sync on advertises semantic + bm25 + hybrid, so the UI offers all.

    The collection is always dense-capable (ADR-031); keyword-only documents
    contribute via the sparse side of the fused query, so all three types are
    offered whenever vector sync is enabled.
    """
    (
        status_pact.upon_receiving("a status request when vector sync is enabled")
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


async def test_status_advertises_nothing_when_vector_sync_disabled(status_pact):
    """Vector sync off advertises no query types, so the UI hides the picker."""
    (
        status_pact.upon_receiving("a status request when vector sync is disabled")
        .given("the server has vector sync disabled")
        .with_request("GET", "/api/v1/status")
        .will_respond_with(200)
        .with_body(
            {"supported_search_types": []},
            content_type="application/json",
        )
    )

    with status_pact.serve() as srv:
        types = await _fetch_supported_search_types(str(srv.url))

    assert types == []


async def _post_search_algorithm(
    base_url: str, path: str, algorithm: str
) -> httpx.Response:
    """Stand-in for astrolabe's McpServerClient search calls: POST ``path`` with an
    explicit ``algorithm``. Returns the raw response so the caller can assert on
    the (strict, ADR-031) 422 the server sends for an unsupported algorithm.

    Both astrolabe search entry points are exercised — ``searchForUnifiedSearch``
    (POST /api/v1/search) and ``search`` (POST /api/v1/vector-viz/search) — which
    on the server share one ``_build_search_algorithm`` gate, so both must reject
    an explicit unsupported algorithm identically.
    """
    async with httpx.AsyncClient(base_url=base_url) as client:
        return await client.post(
            path,
            json={"query": "torch leadership award", "algorithm": algorithm},
        )


@pytest.mark.parametrize("path", ["/api/v1/search", "/api/v1/vector-viz/search"])
async def test_search_rejects_algorithm_when_vector_sync_disabled(
    status_pact, path: str
):
    """Any explicit algorithm against a vector-sync-disabled server → 422 (ADR-031).

    With per-document index mode there is no keyword-only server: whenever vector
    sync is on, all three algorithms are supported. The remaining strict-reject
    case is a request made while vector sync is **off** (``supported_search_types``
    is empty) — the server rejects it with the advertised (empty) set rather than
    silently returning nothing, so astrolabe can surface/guard the error. astrolabe
    also gates client-side from ``/api/v1/status``, but this pins the server-side
    backstop on **both** search endpoints, which share one gate.
    """
    (
        status_pact.upon_receiving(
            f"a semantic search request to {path} when vector sync is disabled"
        )
        .given("the server has vector sync disabled")
        .with_request("POST", path)
        .with_body(
            {"query": "torch leadership award", "algorithm": "semantic"},
            content_type="application/json",
        )
        .will_respond_with(422)
        .with_body(
            {
                "error": "unsupported_search_type",
                "requested": "semantic",
                "supported_search_types": [],
            },
            content_type="application/json",
        )
    )

    with status_pact.serve() as srv:
        resp = await _post_search_algorithm(str(srv.url), path, "semantic")

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_search_type"
    assert body["requested"] == "semantic"
    assert body["supported_search_types"] == []
