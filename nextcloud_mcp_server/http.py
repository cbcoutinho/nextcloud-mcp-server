"""Nextcloud HTTP factories configured from the server's settings.

All outbound connections to Nextcloud (API calls, OIDC endpoints) should use
these factories, so they get consistent TLS settings from the environment
(NEXTCLOUD_VERIFY_SSL, NEXTCLOUD_CA_BUNDLE, NEXTCLOUD_HTTP_KEEPALIVE). They wrap
the configuration-free factories in ``nextcloud_mcp_server.client.http``.
"""

import logging
import ssl
from typing import Any, TypedDict

import httpx2

from .client import NextcloudClient
from .client.http import nextcloud_httpx_client as _nextcloud_httpx_client
from .config import (
    cfg,
    get_nextcloud_http_keepalive,
    get_nextcloud_ssl_verify,
    get_settings,
)

logger = logging.getLogger(__name__)


class NextcloudClientKwargs(TypedDict):
    verify: bool | ssl.SSLContext
    keepalive: bool


def nextcloud_client_kwargs() -> NextcloudClientKwargs:
    """The configured TLS and keep-alive settings for a ``NextcloudClient``.

    Every server-side ``NextcloudClient(...)`` / ``NextcloudClient.from_token(...)``
    splats this in. The client defaults to verifying TLS against the system CAs
    with keep-alive on, so a missed call site ignores ``NEXTCLOUD_VERIFY_SSL``,
    ``NEXTCLOUD_CA_BUNDLE`` and ``NEXTCLOUD_HTTP_KEEPALIVE``.

    Both settings are read eagerly. That is correct because a client builds its
    transport once per lifetime.
    """
    return {
        "verify": get_nextcloud_ssl_verify(),
        "keepalive": get_nextcloud_http_keepalive(),
    }


def nextcloud_httpx_client(**kwargs: Any) -> httpx2.AsyncClient:
    """``client.http.nextcloud_httpx_client`` with the configured TLS settings.

    A caller-supplied ``verify`` kwarg takes precedence.
    """
    kwargs.setdefault("verify", get_nextcloud_ssl_verify())
    return _nextcloud_httpx_client(**kwargs)


def nextcloud_client_from_env() -> NextcloudClient:
    """The single-user ``NextcloudClient`` from settings and env credentials.

    NEXTCLOUD_HOST is read through settings (dynaconf), so a value set only in
    settings.toml is honoured. The credentials stay env/Secret-sourced.
    """
    logger.info("Creating NC Client using settings + env vars")
    host = get_settings().nextcloud_host
    if not host:
        raise ValueError("NEXTCLOUD_HOST must be set to build a client from env")
    username = cfg("NEXTCLOUD_USERNAME")
    password = cfg("NEXTCLOUD_PASSWORD")
    return NextcloudClient(
        base_url=host,
        username=username,
        auth=httpx2.BasicAuth(username, password),
        password=password,
        **nextcloud_client_kwargs(),
    )
