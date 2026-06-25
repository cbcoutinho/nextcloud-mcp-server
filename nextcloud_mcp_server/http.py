"""Centralized HTTP client factory for Nextcloud connections.

All outbound connections to Nextcloud (API calls, OIDC endpoints) should use
these factories to ensure consistent SSL/TLS configuration from environment
variables (NEXTCLOUD_VERIFY_SSL, NEXTCLOUD_CA_BUNDLE).
"""

from typing import Any

import httpx

from .config import get_nextcloud_http_keepalive, get_nextcloud_ssl_verify


def nextcloud_httpx_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with Nextcloud SSL settings applied.

    Reads NEXTCLOUD_VERIFY_SSL and NEXTCLOUD_CA_BUNDLE from the environment
    via ``get_nextcloud_ssl_verify()``. Caller-supplied ``verify`` kwarg
    takes precedence if explicitly provided.

    Args:
        **kwargs: Forwarded to ``httpx.AsyncClient()``.

    Returns:
        Configured ``httpx.AsyncClient``.
    """
    kwargs.setdefault("verify", get_nextcloud_ssl_verify())
    return httpx.AsyncClient(**kwargs)


def nextcloud_httpx_transport(**kwargs: Any) -> httpx.AsyncHTTPTransport:
    """Create an httpx.AsyncHTTPTransport with Nextcloud SSL settings applied.

    Used by ``NextcloudClient`` which wraps the transport in
    ``AsyncDisableCookieTransport``.

    When ``NEXTCLOUD_HTTP_KEEPALIVE=false`` the transport is built with
    ``Limits(max_keepalive_connections=0)`` so every request opens a fresh
    connection instead of reusing a pooled keep-alive one. This prevents a
    truncated/desynced response from poisoning a pooled connection and
    silently returning empty bytes on later reads (see #965). A
    caller-supplied ``limits`` kwarg takes precedence.

    Both ``get_nextcloud_ssl_verify()`` and ``get_nextcloud_http_keepalive()``
    are read eagerly here. That is correct because a transport is built once per
    client lifetime; a future refactor that builds transports per-request would
    turn these into per-request ``get_settings()`` calls and should cache them.

    Args:
        **kwargs: Forwarded to ``httpx.AsyncHTTPTransport()``.

    Returns:
        Configured ``httpx.AsyncHTTPTransport``.
    """
    kwargs.setdefault("verify", get_nextcloud_ssl_verify())
    if not get_nextcloud_http_keepalive():
        kwargs.setdefault("limits", httpx.Limits(max_keepalive_connections=0))
    return httpx.AsyncHTTPTransport(**kwargs)
