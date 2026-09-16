"""Client ID Metadata Documents (CIMD) for the OAuth AS proxy (GH #1470).

draft-ietf-oauth-client-id-metadata-document / MCP SEP-991: a ``client_id``
that is an HTTPS URL names a JSON document, hosted by the client, listing its
``redirect_uris``. The AS fetches that document instead of requiring the client
to be registered (DCR) or pre-configured (``ALLOWED_MCP_CLIENTS``). ChatGPT and
Claude both prefer it when the AS advertises
``client_id_metadata_document_supported``.

Opt-in and fail-closed, like ``ALLOWED_MCP_CLIENTS``: ``CIMD_ALLOWED_HOSTS`` is
a comma-separated list of hostnames whose documents are trusted (``*`` for any
host). Unset means CIMD is disabled and HTTPS ``client_id`` values are treated
as ordinary, unregistered client ids.

The fetch is a server-side request to a URL an unauthenticated caller chose, so
it is SSRF-hardened: HTTPS only, no redirects, every resolved address must be
publicly routable, and the connection is pinned to the address that was
checked (so DNS rebinding between check and connect cannot reach an internal
host), with a bounded body and timeout.
"""

import ipaddress
import json
import logging
import socket
import time
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit

import anyio
import httpx

from nextcloud_mcp_server.config import cfg

logger = logging.getLogger(__name__)

_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_MAX_DOCUMENT_BYTES = 5 * 1024  # draft §6.6 recommends 5 KB
_FETCH_TIMEOUT_SECONDS = 5.0
# Fixed TTL: the draft says to respect Cache-Control, but a short fixed window
# bounds staleness without trusting a header the client controls. Honour
# max-age if a client ever needs faster rotation than this.
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 1000
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class CIMDError(Exception):
    """The client_id's metadata document is unusable. Message is client-safe."""


def _allowed_hosts() -> set[str]:
    raw = str(cfg("CIMD_ALLOWED_HOSTS", "") or "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def cimd_enabled() -> bool:
    """Whether the AS accepts (and advertises) URL-formatted client_ids."""
    return bool(_allowed_hosts())


def is_cimd_client_id(client_id: str) -> bool:
    """Whether *client_id* should be resolved as a metadata document URL."""
    return client_id.startswith("https://") and cimd_enabled()


def _parse_client_id_url(client_id: str) -> SplitResult:
    """Apply the draft's client_id URL rules (§3) and the host trust policy."""
    parts = urlsplit(client_id)
    # Percent-decode before looking for dot segments: "%2e%2e" is a "..", and
    # comparing the raw path would let one through. The URL is an identity, so
    # the draft wants it unambiguous rather than merely safe to fetch.
    segments = [unquote(segment) for segment in parts.path.split("/")]
    try:
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError as e:
        raise CIMDError("client_id URL has an invalid port") from e
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.path in ("", "/")
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
        or "." in segments
        or ".." in segments
    ):
        raise CIMDError("client_id is not a valid Client ID Metadata Document URL")

    hosts = _allowed_hosts()
    if "*" not in hosts and parts.hostname not in hosts:
        raise CIMDError(f"client_id host {parts.hostname!r} is not trusted")
    return parts


def _is_globally_routable(address: str) -> bool:
    """Whether *address* is a public address that reaches no internal host.

    ``ipaddress`` unwraps IPv4-mapped ``::ffff:0:0/96`` itself, but treats the
    whole NAT64 well-known prefix (RFC 6052 ``64:ff9b::/96``) as global no
    matter which IPv4 address its low 32 bits embed. On any DNS64/NAT64 path —
    the normal case on an IPv6-only host — ``64:ff9b::7f00:1`` is translated
    back to ``127.0.0.1``, so the embedded address has to be checked too.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:  # e.g. a scoped link-local "fe80::1%eth0"
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WELL_KNOWN_PREFIX:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global


async def _resolve_public_address(host: str, port: int) -> str:
    """Resolve *host* and refuse it unless every address is publicly routable.

    Checking all addresses, not just the first, closes the trick of publishing
    one public and one internal A record and hoping the client picks the latter.
    """
    try:
        infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise CIMDError(f"cannot resolve client_id host {host!r}") from e

    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise CIMDError(f"cannot resolve client_id host {host!r}")
    for address in addresses:
        if not _is_globally_routable(address):
            raise CIMDError(f"client_id host {host!r} is not publicly routable")
    return addresses[0]


async def _fetch(parts: SplitResult) -> bytes:
    port = parts.port or 443
    hostname = parts.hostname or ""
    address = await _resolve_public_address(hostname, port)
    literal = f"[{address}]" if ":" in address else address
    pinned_url = parts._replace(netloc=f"{literal}:{port}").geturl()

    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET",
                pinned_url,
                headers={"Host": parts.netloc, "Accept": "application/json"},
                # TLS SNI + certificate verification against the real hostname,
                # while the TCP connection goes to the address checked above.
                extensions={"sni_hostname": hostname},
            ) as response:
                if response.status_code != 200:
                    raise CIMDError(
                        f"client metadata document returned HTTP {response.status_code}"
                    )
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_DOCUMENT_BYTES:
                        raise CIMDError("client metadata document is too large")
                return body
    except httpx.HTTPError as e:
        raise CIMDError("failed to fetch client metadata document") from e


def _validate_document(client_id: str, body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(body)
    except ValueError as e:
        raise CIMDError("client metadata document is not valid JSON") from e
    if not isinstance(document, dict):
        raise CIMDError("client metadata document must be a JSON object")
    if document.get("client_id") != client_id:
        raise CIMDError("client metadata document client_id does not match URL")

    redirect_uris = document.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or not all(isinstance(uri, str) for uri in redirect_uris)
    ):
        raise CIMDError("client metadata document has no valid redirect_uris")

    # draft §4.1: a document is public, so it can never carry a shared secret.
    auth_method = document.get("token_endpoint_auth_method")
    if "client_secret" in document or (
        isinstance(auth_method, str) and auth_method.startswith("client_secret")
    ):
        raise CIMDError("client metadata document must not use a shared secret")
    return document


async def get_client_metadata(client_id: str) -> dict[str, Any]:
    """Fetch, validate and cache the metadata document named by *client_id*.

    Raises ``CIMDError`` for any failure. Failures are never cached (draft
    §4.2), so a client that fixes its document is picked up on the next try.
    """
    parts = _parse_client_id_url(client_id)  # before the cache: trust can be revoked
    now = time.monotonic()
    cached = _cache.get(client_id)
    if cached and cached[0] > now:
        return cached[1]

    # One deadline over DNS + connect + body: httpx's timeout is per phase, so a
    # server trickling bytes (or a slow resolver) could otherwise hold this
    # unauthenticated request open far longer.
    try:
        with anyio.fail_after(_FETCH_TIMEOUT_SECONDS):
            body = await _fetch(parts)
    except TimeoutError as e:
        raise CIMDError("timed out fetching client metadata document") from e
    document = _validate_document(client_id, body)

    if len(_cache) >= _CACHE_MAX_ENTRIES:
        # Drop what has already expired before falling back to dumping the lot,
        # so a burst of one-off URLs can't evict live entries on its own.
        for url in [key for key, (expiry, _) in _cache.items() if expiry <= now]:
            del _cache[url]
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            _cache.clear()
    _cache[client_id] = (now + _CACHE_TTL_SECONDS, document)
    logger.info(
        "CIMD: accepted metadata document for %s (client_name=%r)",
        client_id,
        document.get("client_name"),
    )
    return document


async def validate_cimd_client(client_id: str, redirect_uri: str) -> str | None:
    """Validate an authorization request from a CIMD client.

    Returns ``None`` when *redirect_uri* is one the document lists (exact
    match, draft §4.3), otherwise a client-safe error message.
    """
    try:
        document = await get_client_metadata(client_id)
    except CIMDError as e:
        logger.warning("CIMD: rejected client_id %s: %s", client_id, e)
        return str(e)
    if redirect_uri not in document["redirect_uris"]:
        return f"Invalid redirect_uri for client {client_id}"
    return None
