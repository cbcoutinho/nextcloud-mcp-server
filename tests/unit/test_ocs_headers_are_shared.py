"""Guard: ``OCS-APIRequest`` is sent by the httpx client, never per call site.

Nextcloud needs ``OCS-APIRequest: true`` from every non-browser client: OCS
routes answer ``meta.statuscode: 997`` without it, and ``#[CORS]`` app routes
log the session out and re-authenticate, which 404s Tables v1 on Nextcloud 33
(GH #1664). While each client opted in per call, the Tables v1 calls never did.

So the header lives in exactly one place -- ``http.NEXTCLOUD_DEFAULT_HEADERS``,
applied by ``nextcloud_httpx_client`` -- and this module pins both halves of
that: no module spells the header itself, and no module builds a raw
``httpx.AsyncClient`` (which would skip the default) unless it talks to
something other than Nextcloud.

The scans are over the AST rather than over lines, so a dict or call split
across lines by the formatter still counts.
"""

import ast
from pathlib import Path

import httpx
import pytest

import nextcloud_mcp_server as root_pkg
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.client.tables import TablesClient
from nextcloud_mcp_server.http import NEXTCLOUD_DEFAULT_HEADERS, nextcloud_httpx_client

pytestmark = pytest.mark.unit

#: The one module allowed to spell the header and to build a raw client.
DEFINING_MODULE = "http.py"

#: Modules whose raw ``httpx.AsyncClient`` talks to a non-Nextcloud service, so
#: the Nextcloud default headers do not belong on it. Adding a module here is a
#: claim that it never calls Nextcloud -- if it does, use the factory instead.
NON_NEXTCLOUD_CLIENTS = {
    "app.py",  # Qdrant health probe
    "auth/cimd.py",  # client ID metadata documents on the MCP client's host
    "document_processors/collabora.py",
    "document_processors/custom_http.py",
    "document_processors/docling_serve.py",
    "document_processors/ocr.py",
    "document_processors/unstructured.py",
    "providers/gateway.py",
    "providers/gateway_batch.py",
    "providers/ner.py",
    "providers/ollama.py",
    "providers/rerank.py",
    "vector/collection_metadata.py",  # Qdrant
}


def _package_root() -> Path:
    return Path(root_pkg.__file__).parent


def _sources() -> dict[str, ast.Module]:
    """Every module in the distribution, keyed by package-relative POSIX path.

    Bare filenames are not identifiers: ``deck.py`` / ``talk.py`` / ``sharing.py``
    exist under each of ``client/``, ``models/`` and ``server/``.
    """
    root = _package_root()
    return {
        p.relative_to(root).as_posix(): ast.parse(p.read_text())
        for p in sorted(root.rglob("*.py"))
    }


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
        "client/webdav.py",
        "api/apps.py",
        "auth/storage.py",
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
        "OCS-APIRequest is a client default (http.NEXTCLOUD_DEFAULT_HEADERS); "
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
        auth=httpx.BasicAuth("alice", "pw"),
    )
    assert client._client.headers["OCS-APIRequest"] == "true"


async def test_tables_v1_request_carries_the_header_on_the_wire():
    """GH #1664: the Tables v1 calls pass no headers and must still send it."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with nextcloud_httpx_client(
        base_url="https://nc.example.com", transport=httpx.MockTransport(handler)
    ) as http_client:
        await TablesClient(http_client, "alice").get_table_rows(table_id=8)

    assert seen[0].url.path == "/index.php/apps/tables/api/1/tables/8/rows"
    assert seen[0].headers["OCS-APIRequest"] == "true"


def test_the_ocs_json_constant_cannot_be_mutated():
    """Several clients share OCS_REQUEST_HEADERS; an in-place edit must fail."""
    from nextcloud_mcp_server.client.ocs import OCS_REQUEST_HEADERS

    with pytest.raises(TypeError):
        OCS_REQUEST_HEADERS["Accept"] = "text/xml"  # ty: ignore[unsupported-operation]


def test_the_default_headers_cannot_be_mutated():
    """Every Nextcloud client shares NEXTCLOUD_DEFAULT_HEADERS."""
    with pytest.raises(TypeError):
        NEXTCLOUD_DEFAULT_HEADERS["OCS-APIRequest"] = "false"  # ty: ignore[unsupported-operation]
