"""httpx2 client and transport factories for Nextcloud connections.

TLS verification and keep-alive are explicit arguments here. The MCP server
fills them from its configuration (``NEXTCLOUD_VERIFY_SSL``,
``NEXTCLOUD_CA_BUNDLE``, ``NEXTCLOUD_HTTP_KEEPALIVE``) in
``nextcloud_mcp_server.http``.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import httpx2

#: Sent on every request to Nextcloud. A non-browser client identifies itself
#: with ``OCS-APIRequest: true``. OCS routes refuse requests without it
#: (``meta.statuscode: 997``). It also exempts app routes from the CSRF check.
#: On ``#[CORS]`` app routes it stops ``CORSMiddleware`` from logging the
#: session out and re-authenticating, which is the path that 404s Tables v1 on
#: Nextcloud 33 (GH #1664). This is the only place the header is spelled, and
#: per-request headers merge over it. The mapping is read-only for the same
#: reason as ``ocs.OCS_REQUEST_HEADERS``: every client shares this one object.
NEXTCLOUD_DEFAULT_HEADERS: Mapping[str, str] = MappingProxyType(
    {"OCS-APIRequest": "true"}
)


def nextcloud_httpx_client(**kwargs: Any) -> httpx2.AsyncClient:
    """Create an httpx2.AsyncClient that sends ``NEXTCLOUD_DEFAULT_HEADERS``.

    The default headers are client defaults, and caller-supplied ``headers``
    are merged over them.

    This factory is Nextcloud-specific. Build clients for other services
    (Qdrant, the embedding gateway, document processors, ...) with
    ``httpx2.AsyncClient`` directly. The configured OIDC provider is the one
    sanctioned exception: it shares Nextcloud's TLS settings and ignores the
    extra header.

    Args:
        **kwargs: Forwarded to ``httpx2.AsyncClient()``.
    """
    # httpx2.Headers: case-insensitive override, and accepts every header shape
    # httpx2 does (mapping, Headers, list of tuples).
    headers = httpx2.Headers(NEXTCLOUD_DEFAULT_HEADERS)
    headers.update(kwargs.get("headers") or {})
    kwargs["headers"] = headers
    return httpx2.AsyncClient(**kwargs)


def nextcloud_httpx_transport(
    *, keepalive: bool = True, **kwargs: Any
) -> httpx2.AsyncHTTPTransport:
    """Create an httpx2.AsyncHTTPTransport for Nextcloud.

    With ``keepalive=False`` the transport is built with
    ``Limits(max_keepalive_connections=0)``, so every request opens a fresh
    connection instead of reusing a pooled keep-alive one. This stops a
    truncated or desynced response from poisoning a pooled connection and
    silently returning empty bytes on later reads (see #965). A
    caller-supplied ``limits`` kwarg takes precedence.

    Args:
        keepalive: Whether pooled connections may be reused.
        **kwargs: Forwarded to ``httpx2.AsyncHTTPTransport()``.
    """
    if not keepalive:
        kwargs.setdefault("limits", httpx2.Limits(max_keepalive_connections=0))
    return httpx2.AsyncHTTPTransport(**kwargs)
