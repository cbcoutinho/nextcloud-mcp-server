"""Guard: ``OCS-APIRequest`` is sent by the httpx2 client, never per call site.

Nextcloud needs ``OCS-APIRequest: true`` from every non-browser client: OCS
routes answer ``meta.statuscode: 997`` without it, and ``#[CORS]`` app routes
log the session out and re-authenticate, which 404s Tables v1 on Nextcloud 33
(GH #1664). While each client opted in per call, the Tables v1 calls never did.

So the header lives in exactly one place -- ``nextcloud_client.http.NEXTCLOUD_DEFAULT_HEADERS``,
applied by ``nextcloud_httpx_client`` -- and this module pins both halves of
that: no module spells the header itself, and no module builds a raw
``httpx2.AsyncClient`` (which would skip the default) unless it talks to
something other than Nextcloud.

The scans are over the AST rather than over lines, so a dict or call split
across lines by the formatter still counts.
"""

import ast
from pathlib import Path

import httpx2
import nextcloud_client
import pytest
from nextcloud_client import NextcloudClient
from nextcloud_client.http import (
    NEXTCLOUD_DEFAULT_HEADERS,
    nextcloud_httpx_client,
)
from nextcloud_client.tables import TablesClient

import nextcloud_mcp_server

pytestmark = pytest.mark.unit

#: The one module allowed to spell the header and to build a raw client.
DEFINING_MODULE = "nextcloud_client/http.py"

#: Modules whose raw ``httpx2.AsyncClient`` talks to a non-Nextcloud service, so
#: the Nextcloud default headers do not belong on it. Adding a module here is a
#: claim that it never calls Nextcloud -- if it does, use the factory instead.
NON_NEXTCLOUD_CLIENTS = {
    "nextcloud_mcp_server/app.py",  # Qdrant health probe
    "nextcloud_mcp_server/auth/cimd.py",  # client ID metadata documents on the MCP client's host
    "nextcloud_mcp_server/document_processors/collabora.py",
    "nextcloud_mcp_server/document_processors/custom_http.py",
    "nextcloud_mcp_server/document_processors/docling_serve.py",
    "nextcloud_mcp_server/document_processors/ocr.py",
    "nextcloud_mcp_server/document_processors/unstructured.py",
    "nextcloud_mcp_server/providers/gateway.py",
    "nextcloud_mcp_server/providers/gateway_batch.py",
    "nextcloud_mcp_server/providers/ner.py",
    "nextcloud_mcp_server/providers/ollama.py",
    "nextcloud_mcp_server/providers/rerank.py",
    "nextcloud_mcp_server/vector/collection_metadata.py",  # Qdrant
}


#: Both distributions built from this repo: the server and nextcloud-client.
PACKAGES = (nextcloud_mcp_server, nextcloud_client)


def _sources() -> dict[str, ast.Module]:
    """Every module in both packages, keyed ``<package>/<path>`` (POSIX).

    Bare filenames are not identifiers: ``deck.py`` / ``talk.py`` / ``sharing.py``
    exist in ``nextcloud_client`` and under the server's ``models/`` and
    ``server/``.
    """
    sources = {}
    for pkg in PACKAGES:
        root = Path(pkg.__file__).parent
        for p in sorted(root.rglob("*.py")):
            key = f"{pkg.__name__}/{p.relative_to(root).as_posix()}"
            sources[key] = ast.parse(p.read_text())
    return sources


def _header_spellings(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and any(
            isinstance(k, ast.Constant) and k.value == "OCS-APIRequest"
            for k in node.keys
        )
    ]


def _raw_client_calls(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else None
            name = name or (func.id if isinstance(func, ast.Name) else None)
            if name == "AsyncClient":
                lines.append(node.lineno)
    return lines


def test_scan_covers_the_whole_package():
    """Guard the guard: a bad glob would make the scans below vacuously pass."""
    found = set(_sources())
    assert {
        DEFINING_MODULE,
        "nextcloud_client/webdav.py",
        "nextcloud_mcp_server/api/apps.py",
        "nextcloud_mcp_server/auth/storage.py",
    } <= found
    assert _header_spellings(_sources()[DEFINING_MODULE])
    assert _raw_client_calls(_sources()[DEFINING_MODULE])


def test_no_module_spells_the_header():
    offenders = [
        f"{rel}:{line}"
        for rel, tree in _sources().items()
        if rel != DEFINING_MODULE
        for line in _header_spellings(tree)
    ]
    assert not offenders, (
        "OCS-APIRequest is a client default (nextcloud_client.http.NEXTCLOUD_DEFAULT_HEADERS); "
        f"drop it from these call sites: {offenders}"
    )


def test_raw_httpx_clients_are_not_nextcloud_bound():
    offenders = [
        f"{rel}:{line}"
        for rel, tree in _sources().items()
        if rel != DEFINING_MODULE and rel not in NON_NEXTCLOUD_CLIENTS
        for line in _raw_client_calls(tree)
    ]
    assert not offenders, (
        "build Nextcloud-bound clients with http.nextcloud_httpx_client so they "
        f"send NEXTCLOUD_DEFAULT_HEADERS: {offenders}"
    )


def test_allowlist_has_no_stale_entries():
    """A module that stopped building raw clients must leave the allowlist."""
    sources = _sources()
    stale = {
        rel for rel in NON_NEXTCLOUD_CLIENTS if not _raw_client_calls(sources[rel])
    }
    assert not stale, f"no raw AsyncClient left in: {sorted(stale)}"


def test_factory_sends_default_headers_and_merges_caller_headers():
    client = nextcloud_httpx_client(headers={"X-Custom": "1"})
    assert client.headers["OCS-APIRequest"] == "true"
    assert client.headers["X-Custom"] == "1"
    assert dict(NEXTCLOUD_DEFAULT_HEADERS) == {"OCS-APIRequest": "true"}


def test_factory_override_is_case_insensitive_and_accepts_tuple_lists():
    """A caller's header replaces the default whatever its case or shape."""
    client = nextcloud_httpx_client(headers=[("ocs-apirequest", "false")])
    assert client.headers.get_list("OCS-APIRequest") == ["false"]


def test_nextcloud_client_sends_the_header():
    client = NextcloudClient(
        base_url="https://nc.example.com",
        username="alice",
        auth=httpx2.BasicAuth("alice", "pw"),
    )
    assert client._client.headers["OCS-APIRequest"] == "true"


async def test_tables_v1_request_carries_the_header_on_the_wire():
    """GH #1664: the Tables v1 calls pass no headers and must still send it."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=[])

    async with nextcloud_httpx_client(
        base_url="https://nc.example.com", transport=httpx2.MockTransport(handler)
    ) as http_client:
        await TablesClient(http_client, "alice").get_table_rows(table_id=8)

    assert seen[0].url.path == "/index.php/apps/tables/api/1/tables/8/rows"
    assert seen[0].headers["OCS-APIRequest"] == "true"


def test_the_ocs_json_constant_cannot_be_mutated():
    """Several clients share OCS_REQUEST_HEADERS; an in-place edit must fail."""
    from nextcloud_client.ocs import OCS_REQUEST_HEADERS

    with pytest.raises(TypeError):
        OCS_REQUEST_HEADERS["Accept"] = "text/xml"  # ty: ignore[unsupported-operation]


def test_the_default_headers_cannot_be_mutated():
    """Every Nextcloud client shares NEXTCLOUD_DEFAULT_HEADERS."""
    with pytest.raises(TypeError):
        NEXTCLOUD_DEFAULT_HEADERS["OCS-APIRequest"] = "false"  # ty: ignore[unsupported-operation]
