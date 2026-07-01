from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import anyio
import click
import httpx
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as StarletteScope

from nextcloud_mcp_server.admin.payload_backfill import handle_payload_backfill
from nextcloud_mcp_server.api import (
    create_webhook,
    delete_app_password,
    delete_webhook,
    get_app_password_status,
    get_chunk_context,
    get_installed_apps,
    get_pdf_preview,
    get_server_status,
    get_user_access,
    get_user_session,
    get_vector_sync_status,
    list_supported_scopes,
    list_webhooks,
    provision_app_password,
    purge_doc_types_route,
    revoke_user_access,
    unified_search,
    update_user_scopes,
    vector_search,
)
from nextcloud_mcp_server.auth import (
    InsufficientScopeError,
    discover_all_scopes,
    get_access_token_scopes,
    has_required_scopes,
    is_jwt_token,
)
from nextcloud_mcp_server.auth.browser_oauth_routes import (
    oauth_login,
    oauth_login_callback,
    oauth_logout,
)
from nextcloud_mcp_server.auth.client_registration import ensure_oauth_client
from nextcloud_mcp_server.auth.oauth_routes import (
    oauth_as_metadata,
    oauth_authorize,
    oauth_authorize_nextcloud,
    oauth_callback,
    oauth_callback_nextcloud,
    oauth_register_proxy,
    oauth_token_endpoint,
)
from nextcloud_mcp_server.auth.provision_routes import (
    provision_page,
    provision_status,
)
from nextcloud_mcp_server.auth.session_backend import SessionAuthBackend
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage, get_shared_storage
from nextcloud_mcp_server.auth.token_broker import TokenBrokerService
from nextcloud_mcp_server.auth.unified_verifier import UnifiedTokenVerifier
from nextcloud_mcp_server.auth.userinfo_routes import (
    revoke_session,
    user_info_html,
    vector_sync_status_fragment,
)
from nextcloud_mcp_server.auth.viz_routes import (
    chunk_context_endpoint,
    vector_visualization_html,
    vector_visualization_search,
)
from nextcloud_mcp_server.auth.webhook_routes import (
    disable_webhook_preset,
    enable_webhook_preset,
    webhook_management_pane,
)
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import (
    Settings,
    get_document_processor_config,
    get_settings,
)
from nextcloud_mcp_server.config_validators import (
    AuthMode,
    get_mode_summary,
    validate_configuration,
)
from nextcloud_mcp_server.context import get_client as get_nextcloud_client
from nextcloud_mcp_server.http import nextcloud_httpx_client
from nextcloud_mcp_server.observability import (
    ObservabilityMiddleware,
    setup_metrics,
    setup_tracing,
)
from nextcloud_mcp_server.observability.metrics import (
    record_dependency_check,
    set_dependency_health,
)
from nextcloud_mcp_server.observability.readiness import ReadinessCache
from nextcloud_mcp_server.server import (
    AVAILABLE_APPS,
    configure_semantic_tools,
)
from nextcloud_mcp_server.server.auth_tools import register_auth_tools
from nextcloud_mcp_server.server.oauth_tools import register_oauth_tools
from nextcloud_mcp_server.vector.metrics_publisher import vector_sync_metrics_task
from nextcloud_mcp_server.vector.oauth_sync import (
    ProvisionSignal,
    credential_cleanup_task,
    oauth_processor_task,
    user_manager_task,
)
from nextcloud_mcp_server.vector.placeholder import sweep_orphan_placeholders
from nextcloud_mcp_server.vector.processor import processor_task
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client
from nextcloud_mcp_server.vector.queue import build_transport
from nextcloud_mcp_server.vector.scanner import scanner_task
from nextcloud_mcp_server.vector.webhook_receiver import handle_nextcloud_webhook

if TYPE_CHECKING:
    # Annotation-only in this module (the file uses `from __future__ import
    # annotations`, so these are never evaluated at runtime).
    from nextcloud_mcp_server.vector.queue import IngestTransport, TaskProducer

logger = logging.getLogger(__name__)
HTTPXClientInstrumentor().instrument()


def initialize_document_processors():
    """Initialize and register document processors based on configuration.

    This function reads the environment configuration and registers available
    processors (Unstructured, Tesseract, Custom HTTP) with the global registry.
    """
    config = get_document_processor_config()

    if not config["enabled"]:
        logger.info("Document processing disabled")
        return

    # Imported lazily so the API startup path never loads the ingest document
    # stack (document_processors -> pymupdf -> _isolation) unless document
    # processing is actually enabled -- see #877 / the API-vs-ingest split.
    from nextcloud_mcp_server.document_processors import get_registry  # noqa: PLC0415

    registry = get_registry()
    registered_count = 0

    # Register Unstructured processor
    if "unstructured" in config["processors"]:
        unst_config = config["processors"]["unstructured"]
        try:
            from nextcloud_mcp_server.document_processors.unstructured import (  # noqa: PLC0415
                UnstructuredProcessor,
            )

            processor = UnstructuredProcessor(
                api_url=unst_config["api_url"],
                timeout=unst_config["timeout"],
                default_strategy=unst_config["strategy"],
                default_languages=unst_config["languages"],
                progress_interval=unst_config.get("progress_interval", 10),
            )
            registry.register(processor, priority=10)
            logger.info("Registered Unstructured processor: %s", unst_config["api_url"])
            registered_count += 1
        except Exception as e:
            logger.warning("Failed to register Unstructured processor: %s", e)

    # Register Tesseract processor
    if "tesseract" in config["processors"]:
        tess_config = config["processors"]["tesseract"]
        try:
            from nextcloud_mcp_server.document_processors.tesseract import (  # noqa: PLC0415
                TesseractProcessor,
            )

            processor = TesseractProcessor(
                tesseract_cmd=tess_config.get("tesseract_cmd"),
                default_lang=tess_config["lang"],
            )
            registry.register(processor, priority=5)
            logger.info("Registered Tesseract processor: lang=%s", tess_config["lang"])
            registered_count += 1
        except Exception as e:
            logger.warning("Failed to register Tesseract processor: %s", e)

    # Register PyMuPDF processor (high priority, local, no API required)
    if "pymupdf" in config["processors"]:
        pymupdf_config = config["processors"]["pymupdf"]
        try:
            from nextcloud_mcp_server.document_processors.pymupdf import (  # noqa: PLC0415
                PyMuPDFProcessor,
            )

            processor = PyMuPDFProcessor(
                extract_images=pymupdf_config.get("extract_images", True),
                image_dir=pymupdf_config.get("image_dir"),
            )
            registry.register(processor, priority=15)  # Higher than unstructured
            logger.info(
                "Registered PyMuPDF processor: extract_images=%s",
                pymupdf_config.get("extract_images", True),
            )
            registered_count += 1
        except Exception as e:
            logger.warning("Failed to register PyMuPDF processor: %s", e)

    # Register custom processor
    if "custom" in config["processors"]:
        custom_config = config["processors"]["custom"]
        try:
            from nextcloud_mcp_server.document_processors.custom_http import (  # noqa: PLC0415
                CustomHTTPProcessor,
            )

            processor = CustomHTTPProcessor(
                name=custom_config["name"],
                api_url=custom_config["api_url"],
                api_key=custom_config.get("api_key"),
                timeout=custom_config["timeout"],
                supported_types=custom_config["supported_types"],
            )
            registry.register(processor, priority=1)
            logger.info(
                "Registered Custom processor '%s': %s",
                custom_config["name"],
                custom_config["api_url"],
            )
            registered_count += 1
        except Exception as e:
            logger.warning("Failed to register Custom processor: %s", e)

    if registered_count > 0:
        logger.info(
            "Document processing initialized with %s processor(s): %s",
            registered_count,
            ", ".join(registry.list_processors()),
        )
    else:
        logger.warning("Document processing enabled but no processors registered")


def validate_pkce_support(discovery: dict, discovery_url: str) -> None:
    """
    Validate that the OIDC provider properly advertises PKCE support.

    According to RFC 8414, if code_challenge_methods_supported is absent,
    it means the authorization server does not support PKCE.

    MCP clients require PKCE with S256 and will refuse to connect if this
    field is missing or doesn't include S256.
    """

    code_challenge_methods = discovery.get("code_challenge_methods_supported")

    if code_challenge_methods is None:
        click.echo("=" * 80, err=True)
        click.echo(
            "ERROR: OIDC CONFIGURATION ERROR - Missing PKCE Support Advertisement",
            err=True,
        )
        click.echo("=" * 80, err=True)
        click.echo(f"Discovery URL: {discovery_url}", err=True)
        click.echo("", err=True)
        click.echo(
            "The OIDC discovery document is missing 'code_challenge_methods_supported'.",
            err=True,
        )
        click.echo(
            "According to RFC 8414, this means the server does NOT support PKCE.",
            err=True,
        )
        click.echo("", err=True)
        click.echo("⚠️  MCP clients (like Claude Code) WILL REJECT this provider!")
        click.echo("", err=True)
        click.echo("How to fix:", err=True)
        click.echo(
            "  1. Ensure PKCE is enabled in Nextcloud OIDC app settings", err=True
        )
        click.echo(
            "  2. Update the OIDC app to advertise PKCE support in discovery", err=True
        )
        click.echo("  3. See: RFC 8414 Section 2 (Authorization Server Metadata)")
        click.echo("=" * 80, err=True)
        click.echo("", err=True)
        return

    if "S256" not in code_challenge_methods:
        click.echo("=" * 80, err=True)
        click.echo(
            "WARNING: OIDC CONFIGURATION WARNING - S256 Challenge Method Not Advertised",
            err=True,
        )
        click.echo("=" * 80, err=True)
        click.echo(f"Discovery URL: {discovery_url}", err=True)
        click.echo(f"Advertised methods: {code_challenge_methods}", err=True)
        click.echo("", err=True)
        click.echo("MCP specification requires S256 code challenge method.", err=True)
        click.echo("Some clients may reject this provider.", err=True)
        click.echo("=" * 80, err=True)
        click.echo("", err=True)
        return

    click.echo(f"✓ PKCE support validated: {code_challenge_methods}")


@dataclass
class VectorSyncState:
    """
    Module-level state for vector sync background tasks.

    This singleton bridges the Starlette server lifespan (where background tasks run)
    and FastMCP session lifespans (where MCP tools need access to the streams).
    """

    document_send_stream: MemoryObjectSendStream | None = None
    document_receive_stream: MemoryObjectReceiveStream | None = None
    # Ingest producer the scanner/webhook send to: the in-memory send stream
    # (INGEST_QUEUE=memory) or the procrastinate producer (INGEST_QUEUE=postgres,
    # Deck #183). The webhook reads this; in memory mode it is the same object as
    # document_send_stream.
    task_producer: "TaskProducer | None" = None
    shutdown_event: anyio.Event | None = None
    scanner_wake_event: anyio.Event | None = None
    # Rung by a provisioning request to wake ``user_manager_task`` immediately so
    # a just-provisioned user's scanner is spawned without waiting out the
    # ``VECTOR_SYNC_USER_POLL_INTERVAL`` poll. ``None`` when no user manager is
    # running (single-user mode or vector sync disabled), in which case
    # ``notify_user_provisioned`` is a no-op.
    provision_signal: "ProvisionSignal | None" = None
    # Long-lived task group used for fire-and-forget background work spawned
    # from the request path (e.g. ADR-019 verify-on-read eviction). Set by the
    # starlette lifespan after entering its task group; cleared on shutdown.
    eviction_task_group: TaskGroup | None = None


# Module-level singleton for vector sync state
_vector_sync_state = VectorSyncState()


def notify_user_provisioned() -> None:
    """Wake the user manager to discover a just-provisioned user immediately.

    Provisioning call sites invoke this after a successful app-password store so
    ``user_manager_task`` re-polls at once instead of waiting out
    ``VECTOR_SYNC_USER_POLL_INTERVAL``. The 60s poll remains the backstop, so a
    missed signal (e.g. provisioning handled on a different replica than the
    manager) only delays the scan, never skips it.

    No-op when ``provision_signal`` is ``None`` — single-user mode or vector
    sync disabled, where no user manager is running.
    """
    signal = _vector_sync_state.provision_signal
    if signal is not None:
        signal.ring()


def _wire_vector_sync_state(
    app: Starlette,
    transport: IngestTransport,
    shutdown_event: anyio.Event,
    scanner_wake_event: anyio.Event,
) -> None:
    """Publish the ingest transport + sync events to every state surface.

    Both lifespan paths (single-user and multi-user) previously duplicated these
    writes three times each: ``app.state``, the module singleton
    ``_vector_sync_state`` (read by FastMCP session lifespans), and the mounted
    ``/app`` browser sub-app. ``document_send_stream``/``document_receive_stream``
    come from the transport — ``None`` in postgres mode (no in-process stream) —
    and ``task_producer`` is the transport's producer in both modes (Deck #183,
    ADR-028).

    ``eviction_task_group`` is deliberately not set here: it only exists once the
    lifespan has entered its ``anyio.create_task_group()`` (after this call), so
    the lifespan assigns it on the singleton directly at that point.
    ``provision_signal`` is likewise excluded on purpose — only ``user_manager_task``
    consumes it (request handlers reach it via ``notify_user_provisioned``), so the
    multi-user lifespan sets it on the singleton directly rather than fanning it out
    to ``app.state``/the browser sub-app.
    """
    send_stream = transport.send_stream
    receive_stream = transport.receive_stream
    task_producer = transport.producer

    def _apply(state: Any) -> None:
        state.document_send_stream = send_stream
        state.document_receive_stream = receive_stream
        state.task_producer = task_producer
        state.shutdown_event = shutdown_event
        state.scanner_wake_event = scanner_wake_event

    # app.state (Starlette) + the module singleton share the same attribute names.
    _apply(app.state)
    _apply(_vector_sync_state)
    logger.info("Vector sync state published (app.state + module singleton)")

    # Also share with the mounted /app browser sub-app, if present.
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/app":
            browser_app = cast(Starlette, route.app)
            _apply(browser_app.state)
            logger.info("Vector sync state shared with browser_app for /app")
            break


def _clear_vector_sync_state() -> None:
    """Drop the module-singleton ingest references on lifespan shutdown.

    Mirrors the ``eviction_task_group = None`` cleanup so that any code reaching
    the singleton in the narrow window between transport teardown and process
    exit (e.g. a late webhook) sees ``None`` rather than a producer/stream backed
    by an already-closed resource. The per-request ``shutdown_event`` gate is the
    primary guard; this is defense-in-depth. Integration tests with module-level
    singletons also benefit (no stale closed producer leaks between runs).
    """
    _vector_sync_state.task_producer = None
    _vector_sync_state.document_send_stream = None
    _vector_sync_state.document_receive_stream = None
    # Symmetric with the fields above: the just-fired events belong to the
    # closed lifespan; the next startup's _wire_vector_sync_state rebinds them.
    _vector_sync_state.shutdown_event = None
    _vector_sync_state.scanner_wake_event = None
    _vector_sync_state.provision_signal = None


# =============================================================================
# Readiness dependency health (Deck #302)
# =============================================================================
#
# Readiness must reflect "this process is up and configured to serve", NOT the
# live reachability of shared external dependencies. The MCP server typically
# runs as a single replica per tenant; failing readiness when Nextcloud or
# Qdrant blips would pull the only Pod out of its Service, leaving the gateway
# with no upstream and turning a degraded dependency into a total outage plus
# an MCP reconnect storm. So external dependency health is refreshed by a
# background loop, cached, and *reported but non-gating* — and the probe path
# never performs external I/O.


def _default_mcp_server_url() -> str:
    """Fallback MCP server URL (OAuth audience) when NEXTCLOUD_MCP_SERVER_URL is
    unset — derived from the configured PORT so a custom port is honoured."""
    return f"http://localhost:{get_settings().port}"


# Pre-loop default; _readiness_refresh_loop overrides ttl_seconds at startup to
# 2x the configured refresh interval, so bumping this value alone has no effect.
_readiness_cache = ReadinessCache(ttl_seconds=30.0)


async def _check_nextcloud_health() -> None:
    """Probe Nextcloud ``status.php`` and record the result in the cache.

    Catches everything: the refresh loop must never crash on a dependency
    error, and a failed check is just an unhealthy status, not an exception.
    """
    host = get_settings().nextcloud_host
    if not host:
        return
    start = time.time()
    try:
        async with nextcloud_httpx_client(timeout=2.0) as client:
            response = await client.get(f"{host}/status.php")
        healthy = response.status_code == 200
        detail = "ok" if healthy else f"error: status {response.status_code}"
    except Exception as e:  # noqa: BLE001 - any failure is "unhealthy"
        healthy = False
        detail = f"error: {e}"
    _readiness_cache.update("nextcloud_reachable", healthy, detail)
    set_dependency_health("nextcloud", healthy)
    record_dependency_check("nextcloud", time.time() - start)


async def _check_qdrant_health() -> None:
    """Probe Qdrant ``/readyz`` (network mode) and record the result.

    Qdrant Cloud's auth gateway 403s unauthenticated requests, so forward the
    same api-key the configured client uses (see vector/qdrant_client.py).
    """
    settings = get_settings()
    qdrant_url = settings.qdrant_url
    if not qdrant_url:
        return
    headers = {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else {}
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{qdrant_url}/readyz", headers=headers)
        healthy = response.status_code == 200
        detail = "ok" if healthy else f"error: status {response.status_code}"
    except Exception as e:  # noqa: BLE001 - any failure is "unhealthy"
        healthy = False
        detail = f"error: {e}"
    _readiness_cache.update("qdrant", healthy, detail)
    set_dependency_health("qdrant", healthy)
    record_dependency_check("qdrant", time.time() - start)


async def _refresh_dependency_health() -> None:
    """Refresh all external dependency statuses concurrently (one pass)."""
    settings = get_settings()
    async with anyio.create_task_group() as tg:
        tg.start_soon(_check_nextcloud_health)
        if settings.vector_sync_enabled and settings.qdrant_url:
            tg.start_soon(_check_qdrant_health)
        elif settings.vector_sync_enabled:
            # Embedded Qdrant (memory/persistent mode) — no external service.
            _readiness_cache.update("qdrant", True, "embedded")
            set_dependency_health("qdrant", True)


async def _readiness_refresh_loop(*, task_status=anyio.TASK_STATUS_IGNORED) -> None:
    """Background loop that keeps ``_readiness_cache`` warm off the probe path.

    Reports its own ``CancelScope`` via ``task_status`` so the lifespan can stop
    just this infinite loop at shutdown while the sync tasks drain naturally on
    their ``shutdown_event``.
    """
    interval = get_settings().health_ready_refresh_interval
    # Keep the staleness window in step with the configured cadence so
    # is_stale() stays meaningful when the interval is tuned off its default.
    _readiness_cache.ttl_seconds = interval * 2
    # Drop entries from a prior lifespan run in the same process (the integration
    # matrix restarts the server) so the snapshot reflects only this run's deps.
    _readiness_cache.statuses.clear()
    logger.info(
        "Readiness dependency-health refresh loop started (every %ss)", interval
    )
    with anyio.CancelScope() as scope:
        task_status.started(scope)
        while True:
            try:
                await _refresh_dependency_health()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.warning(
                    "Readiness dependency refresh iteration failed", exc_info=True
                )
            await anyio.sleep(interval)


@dataclass
class AppContext:
    """Application context for BasicAuth mode."""

    client: NextcloudClient
    storage: "RefreshTokenStorage | None" = None
    document_send_stream: MemoryObjectSendStream | None = None
    document_receive_stream: MemoryObjectReceiveStream | None = None
    shutdown_event: anyio.Event | None = None
    scanner_wake_event: anyio.Event | None = None

    @property
    def task_producer(self) -> "TaskProducer | None":
        # Read dynamically from the module-level singleton (like
        # eviction_task_group) rather than snapshotting at yield time — that way
        # a session can't observe a stale ``None`` and the per-session yields
        # can't forget to forward it (the bug this property replaces). The
        # vector-sync status tool reads this for postgres-backend job counts.
        return _vector_sync_state.task_producer

    @property
    def eviction_task_group(self) -> TaskGroup | None:
        # Read dynamically from the module-level singleton instead of
        # snapshotting at lifespan-yield time. Snapshotting is order-sensitive:
        # if the FastMCP server lifespan ever runs before the Starlette
        # lifespan assigns the task group, every session for the life of the
        # process would see ``None`` and fall back to inline eviction.
        return _vector_sync_state.eviction_task_group


@dataclass
class OAuthAppContext:
    """Application context for OAuth mode."""

    nextcloud_host: str
    token_verifier: object  # UnifiedTokenVerifier (ADR-005 compliant)
    refresh_token_storage: "RefreshTokenStorage | None" = None
    oauth_client: object | None = None
    oauth_provider: str = "nextcloud"  # "nextcloud" or "keycloak"
    server_client_id: str | None = None  # MCP server's OAuth client ID (static or DCR)
    document_send_stream: MemoryObjectSendStream | None = None
    document_receive_stream: MemoryObjectReceiveStream | None = None
    shutdown_event: anyio.Event | None = None
    scanner_wake_event: anyio.Event | None = None

    @property
    def task_producer(self) -> "TaskProducer | None":
        # See AppContext.task_producer for rationale.
        return _vector_sync_state.task_producer

    @property
    def eviction_task_group(self) -> TaskGroup | None:
        # See AppContext.eviction_task_group for rationale.
        return _vector_sync_state.eviction_task_group


class BasicAuthMiddleware:
    """Middleware to extract BasicAuth credentials from Authorization header.

    For multi-user BasicAuth pass-through mode, this middleware extracts
    username/password from the Authorization: Basic header and stores them
    in the request state for use by the context layer.

    The credentials are NOT stored persistently - they are passed through
    directly to Nextcloud APIs for each request (stateless).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(
        self, scope: StarletteScope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "http":
            # Extract Authorization header
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"")

            if auth_header.startswith(b"Basic "):
                try:
                    # Decode base64(username:password)
                    encoded = auth_header[6:]  # Skip "Basic "
                    decoded = base64.b64decode(encoded).decode("utf-8")
                    username, password = decoded.split(":", 1)

                    # Store in request state
                    scope.setdefault("state", {})
                    scope["state"]["basic_auth"] = {
                        "username": username,
                        "password": password,
                    }
                    logger.debug(
                        "BasicAuth credentials extracted for user: %s", username
                    )
                except Exception as e:
                    logger.warning("Failed to extract BasicAuth credentials: %s", e)

        await self.app(scope, receive, send)


async def load_oauth_client_credentials(
    nextcloud_host: str, registration_endpoint: str | None
) -> tuple[str, str]:
    """
    Load OAuth client credentials from environment, storage file, or dynamic registration.

    This consolidates the client loading logic that was duplicated across multiple functions.

    Args:
        nextcloud_host: Nextcloud instance URL
        registration_endpoint: Dynamic registration endpoint URL (or None if not available)

    Returns:
        Tuple of (client_id, client_secret)

    Raises:
        ValueError: If credentials cannot be obtained
    """
    # Try environment variables first
    client_id = get_settings().oidc_client_id
    client_secret = get_settings().oidc_client_secret

    if client_id and client_secret:
        logger.info("Using pre-configured OAuth client credentials from environment")
        return (client_id, client_secret)

    # Try loading from SQLite storage
    try:
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        client_data = await storage.get_oauth_client()
        if client_data:
            logger.info(
                "Loaded OAuth client from SQLite: %s...", client_data["client_id"][:16]
            )
            return (client_data["client_id"], client_data["client_secret"])
    except ValueError:
        # TOKEN_ENCRYPTION_KEY not set, skip SQLite storage check
        logger.debug("SQLite storage not available (TOKEN_ENCRYPTION_KEY not set)")

    # Try dynamic registration if available
    if registration_endpoint:
        logger.info("Dynamic client registration available")
        mcp_server_url = (
            get_settings().nextcloud_mcp_server_url or _default_mcp_server_url()
        )
        redirect_uris = [
            f"{mcp_server_url}/oauth/callback",  # Unified callback (flow determined by query param)
        ]

        # MCP server DCR: Register with ALL supported scopes
        # When we register as a resource server (with resource_url), the allowed_scopes
        # represent what scopes are AVAILABLE for this resource, not what the server needs.
        # External clients will request tokens with resource=http://localhost:8001/mcp
        # and the authorization server will limit them to these allowed scopes.
        #
        # The PRM endpoint advertises the same scopes dynamically via @require_scopes decorators.
        # These must stay in sync — any scope a tool uses via @require_scopes must be listed here.
        dcr_scopes = (
            "openid profile email "
            "notes.read notes.write calendar.read calendar.write todo.read todo.write "
            "contacts.read contacts.write cookbook.read cookbook.write deck.read deck.write "
            "tables.read tables.write files.read files.write sharing.read sharing.write "
            "news.read news.write collectives.read collectives.write"
        )

        # Add conditional scopes based on server configuration
        dcr_settings = get_settings()

        # semantic.read gates MCP-server-level semantic search tools
        if dcr_settings.vector_sync_enabled:
            dcr_scopes = f"{dcr_scopes} semantic.read"
            logger.info("✓ semantic.read scope enabled for semantic search tools")

        # offline_access enables refresh tokens for background operations
        enable_offline_access = dcr_settings.enable_offline_access
        if enable_offline_access:
            dcr_scopes = f"{dcr_scopes} offline_access"
            logger.info("✓ offline_access scope enabled for refresh tokens")

        logger.info("MCP server DCR scopes (resource server): %s", dcr_scopes)

        # Get token type from environment (Bearer or jwt)
        # Note: Must be lowercase "jwt" to match OIDC app's check
        token_type = get_settings().oidc_token_type.lower()
        # Special case: "bearer" should remain capitalized for compatibility
        if token_type != "jwt":
            token_type = "Bearer"
        logger.info("Requesting token type: %s", token_type)

        # Ensure OAuth client in SQLite storage
        storage = RefreshTokenStorage.from_env()
        await storage.initialize()

        # RFC 9728: resource_url must be a URL for the protected resource
        # This URL is used by token introspection to match tokens to this client
        resource_url = f"{mcp_server_url}/mcp"

        client_info = await ensure_oauth_client(
            nextcloud_url=nextcloud_host,
            registration_endpoint=registration_endpoint,
            storage=storage,
            client_name=f"Nextcloud MCP Server ({token_type})",
            redirect_uris=redirect_uris,
            scopes=dcr_scopes,  # Use DCR-specific scopes (basic OIDC only)
            token_type=token_type,
            resource_url=resource_url,  # RFC 9728 Protected Resource URL
        )

        logger.info("OAuth client ready: %s...", client_info.client_id[:16])
        return (client_info.client_id, client_info.client_secret)

    # No credentials available
    raise ValueError(
        "OAuth mode requires either:\n"
        "1. NEXTCLOUD_OIDC_CLIENT_ID and NEXTCLOUD_OIDC_CLIENT_SECRET environment variables, OR\n"
        "2. Pre-existing client credentials in SQLite storage (TOKEN_STORAGE_DB), OR\n"
        "3. Dynamic client registration enabled on Nextcloud OIDC app\n\n"
        "Note: TOKEN_ENCRYPTION_KEY is required for SQLite storage"
    )


@asynccontextmanager
async def app_lifespan_basic(server: FastMCP) -> AsyncIterator[AppContext]:
    """
    Manage application lifecycle for BasicAuth mode (FastMCP session lifespan).

    For single-user mode: Creates a single Nextcloud client with basic authentication
    that is shared across all requests within a session.

    For multi-user mode: No shared client - clients created per-request by BasicAuthMiddleware.

    Note: Background tasks (scanner, processor) are started at server level
    in starlette_lifespan, not here. This lifespan runs per-session.
    """
    settings = get_settings()
    is_multi_user = settings.enable_multi_user_basic_auth

    logger.info(
        "Starting MCP session in %s BasicAuth mode",
        "multi-user" if is_multi_user else "single-user",
    )

    # Only create shared client for single-user mode
    client = None
    if not is_multi_user:
        logger.info("Creating shared Nextcloud client with BasicAuth")
        client = NextcloudClient.from_env()
        logger.info("Client initialization complete")
    else:
        logger.info(
            "Multi-user mode - clients created per-request from BasicAuth headers"
        )

    # Initialize persistent storage (for webhook tracking and future features)
    storage = RefreshTokenStorage.from_env()
    await storage.initialize()
    logger.info("Persistent storage initialized (webhook tracking enabled)")

    # Initialize document processors
    initialize_document_processors()

    # Yield client context - scanner runs at server level (starlette_lifespan)
    # Include vector sync state from module singleton (set by starlette_lifespan)
    try:
        yield AppContext(
            client=client,  # type: ignore[arg-type]  # None in multi-user mode
            storage=storage,
            document_send_stream=_vector_sync_state.document_send_stream,
            document_receive_stream=_vector_sync_state.document_receive_stream,
            shutdown_event=_vector_sync_state.shutdown_event,
            scanner_wake_event=_vector_sync_state.scanner_wake_event,
            # task_producer and eviction_task_group are exposed via @property
            # (read _vector_sync_state at access time, not snapshot).
        )
    finally:
        logger.info("Shutting down BasicAuth session")
        if client is not None:
            await client.close()
        # Dispose the storage engine so pooled asyncpg connections drain
        # cleanly on SIGTERM (ADR-026, PR #798 round-4).
        try:
            await storage.close()
        except Exception as e:
            logger.warning("Error disposing storage: %s", e)


async def _perform_oidc_discovery(
    discovery_url: str, settings: Settings, timeout: float | None = None
) -> dict[str, Any]:
    """Fetch the OIDC discovery document, retrying transient failures.

    OIDC discovery runs synchronously at startup and is fatal on failure. On a
    freshly-scheduled pod the network egress path (e.g. Cilium ``toFQDNs`` allow
    + egress-gateway SNAT programming) can take a few seconds to converge, during
    which the request is silently dropped and times out. Without retries a
    cold-start race crashloops the backend on the very first attempt; retrying
    with capped exponential backoff + full jitter lets startup ride out that
    window instead.

    Transport errors (connect/read timeouts, connection resets) and 5xx
    responses are treated as transient and retried. A 4xx response is a
    configuration error, not a transient condition, so it is raised immediately.

    ``timeout`` overrides the per-attempt httpx timeout (seconds); ``None`` keeps
    httpx's default. The hybrid multi-user-basic path passes an explicit,
    longer budget here so it keeps its original per-attempt timeout while also
    gaining the retries.
    """
    # Clamp defensively: Settings can be constructed directly (e.g. in tests),
    # bypassing the gte=1/gte=0 dynaconf validators. A negative backoff would
    # otherwise flip random.uniform(0, delay) into a reversed range.
    max_attempts = max(1, settings.oidc_discovery_max_attempts)
    backoff_base = max(0.0, settings.oidc_discovery_backoff_base)
    backoff_max = max(0.0, settings.oidc_discovery_backoff_max)

    # Pass timeout through only when set — httpx treats an explicit
    # timeout=None as "disable timeout" rather than "use the default".
    client_kwargs: dict[str, Any] = {"follow_redirects": True}
    if timeout is not None:
        client_kwargs["timeout"] = timeout

    for attempt in range(1, max_attempts + 1):
        try:
            async with nextcloud_httpx_client(**client_kwargs) as client:
                response = await client.get(discovery_url)
                response.raise_for_status()
                return response.json()
        except (
            httpx.RequestError,
            httpx.HTTPStatusError,
            json.JSONDecodeError,
        ) as exc:
            # httpx.RequestError is the broad network-layer base (transport
            # timeouts/connection errors plus TooManyRedirects, DecodingError,
            # …); a malformed 200 body (e.g. a proxy/gateway "warming up" HTML
            # placeholder served during cold start) raises JSONDecodeError.
            # Treat all of these as transient like a 5xx, otherwise they
            # reintroduce the very crashloop this retry loop exists to prevent.
            # 4xx is a misconfiguration (wrong URL, no OIDC app) — fail fast so
            # the operator sees the real error instead of a retry storm.
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and not 500 <= exc.response.status_code < 600
            ):
                raise
            if attempt >= max_attempts:
                logger.exception("OIDC discovery failed after %d attempt(s)", attempt)
                raise
            # Full jitter over a capped exponential backoff.
            delay = min(backoff_max, backoff_base * 2 ** (attempt - 1))
            sleep_for = random.uniform(0, delay)
            logger.warning(
                "OIDC discovery attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                max_attempts,
                type(exc).__name__,
                sleep_for,
            )
            await anyio.sleep(sleep_for)

    # Unreachable: the loop always returns a document or raises above.
    raise RuntimeError("OIDC discovery retry loop exited without a result")


async def setup_oauth_config():
    """
    Setup OAuth configuration by performing OIDC discovery and client registration.

    Auto-detects OAuth provider mode:
    - Integrated mode: OIDC_DISCOVERY_URL points to NEXTCLOUD_HOST (or not set)
      → Nextcloud OIDC app provides both OAuth and API access
    - External IdP mode: OIDC_DISCOVERY_URL points to external provider
      → External IdP for OAuth, Nextcloud user_oidc validates tokens and provides API access

    Uses OIDC environment variables:
    - OIDC_DISCOVERY_URL: OIDC discovery endpoint (optional, defaults to NEXTCLOUD_HOST)
    - NEXTCLOUD_OIDC_CLIENT_ID / NEXTCLOUD_OIDC_CLIENT_SECRET: Static credentials (optional, uses DCR if not provided)
    - NEXTCLOUD_OIDC_SCOPES: Requested OAuth scopes

    This is done synchronously before FastMCP initialization because FastMCP
    requires token_verifier at construction time.

    Returns:
        Tuple of (nextcloud_host, token_verifier, auth_settings, refresh_token_storage, oauth_client, oauth_provider, client_id, client_secret)
    """
    # Get settings for enable_offline_access check (handles both ENABLE_BACKGROUND_OPERATIONS
    # and ENABLE_OFFLINE_ACCESS environment variables)
    settings = get_settings()

    nextcloud_host = settings.nextcloud_host
    if not nextcloud_host:
        raise ValueError(
            "NEXTCLOUD_HOST environment variable is required for OAuth mode"
        )

    nextcloud_host = nextcloud_host.rstrip("/")

    # Get OIDC discovery URL (defaults to Nextcloud integrated mode)
    discovery_url = (
        settings.oidc_discovery_url
        or f"{nextcloud_host}/.well-known/openid-configuration"
    )
    logger.info("Performing OIDC discovery: %s", discovery_url)

    # Perform OIDC discovery (retries transient failures — see helper docstring)
    discovery = await _perform_oidc_discovery(discovery_url, settings)

    logger.info("✓ OIDC discovery successful")

    # Validate PKCE support
    validate_pkce_support(discovery, discovery_url)

    # Extract OIDC endpoints
    issuer = discovery["issuer"]
    userinfo_uri = discovery["userinfo_endpoint"]
    jwks_uri = discovery.get("jwks_uri")
    introspection_uri = discovery.get("introspection_endpoint")
    registration_endpoint = discovery.get("registration_endpoint")

    logger.info("OIDC endpoints discovered:")
    logger.info("  Issuer: %s", issuer)
    logger.info("  Userinfo: %s", userinfo_uri)
    if jwks_uri:
        logger.info("  JWKS: %s", jwks_uri)
    if introspection_uri:
        logger.info("  Introspection: %s", introspection_uri)

    # Auto-detect provider mode based on issuer
    # External IdP mode: issuer doesn't match Nextcloud host
    # Normalize URLs for comparison (handle port differences like :80 for HTTP)
    def normalize_url(url: str) -> str:
        """Normalize URL by removing default ports (80 for HTTP, 443 for HTTPS)."""
        parsed = urlparse(url)
        # Remove default ports
        if (parsed.scheme == "http" and parsed.port == 80) or (
            parsed.scheme == "https" and parsed.port == 443
        ):
            # Remove explicit default port
            hostname = parsed.hostname or parsed.netloc.split(":")[0]
            return f"{parsed.scheme}://{hostname}"
        return f"{parsed.scheme}://{parsed.netloc}"

    issuer_normalized = normalize_url(issuer)
    nextcloud_normalized = normalize_url(nextcloud_host)

    # Determine if this is an external IdP by comparing discovered issuer with Nextcloud host
    is_external_idp = not issuer_normalized.startswith(nextcloud_normalized)

    if is_external_idp:
        oauth_provider = "external"  # Could be Keycloak, Auth0, Okta, etc.
        logger.info(
            "✓ Detected external IdP mode (issuer: %s != Nextcloud: %s)",
            issuer,
            nextcloud_host,
        )
        logger.info("  Tokens will be validated via Nextcloud user_oidc app")
    else:
        oauth_provider = "nextcloud"
        logger.info("✓ Detected integrated mode (Nextcloud OIDC app)")

    # Check if offline access (refresh tokens) is enabled
    # Use settings.enable_offline_access which handles both ENABLE_BACKGROUND_OPERATIONS (new)
    # and ENABLE_OFFLINE_ACCESS (deprecated) environment variables
    enable_offline_access = settings.enable_offline_access

    # Initialize refresh token storage if enabled
    refresh_token_storage = None
    if enable_offline_access:
        try:
            # Validate encryption key before initializing
            encryption_key = settings.token_encryption_key
            if not encryption_key:
                logger.warning(
                    "ENABLE_OFFLINE_ACCESS=true but TOKEN_ENCRYPTION_KEY not set. "
                    "Refresh tokens will NOT be stored. Generate a key with:\n"
                    '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
                )
            else:
                refresh_token_storage = RefreshTokenStorage.from_env()
                await refresh_token_storage.initialize()
                logger.info(
                    "✓ Refresh token storage initialized (offline_access enabled)"
                )
        except Exception as e:
            logger.error("Failed to initialize refresh token storage: %s", e)
            logger.warning(
                "Continuing without refresh token storage - users will need to re-authenticate after token expiration"
            )

    # Load client credentials (static or dynamic registration)
    client_id = settings.oidc_client_id
    client_secret = settings.oidc_client_secret

    if client_id and client_secret:
        logger.info("Using static OIDC client credentials: %s", client_id)
    elif registration_endpoint:
        logger.info(
            "NEXTCLOUD_OIDC_CLIENT_ID not set, attempting Dynamic Client Registration"
        )
        client_id, client_secret = await load_oauth_client_credentials(
            nextcloud_host=nextcloud_host, registration_endpoint=registration_endpoint
        )
    else:
        raise ValueError(
            "NEXTCLOUD_OIDC_CLIENT_ID and NEXTCLOUD_OIDC_CLIENT_SECRET environment variables are required "
            "when the OIDC provider does not support Dynamic Client Registration. "
            f"Discovery URL: {discovery_url}"
        )

    # ADR-005: Unified Token Verifier with proper audience validation
    # Use public issuer URL for JWT validation if set (handles Docker internal/external URL mismatch)
    # Tokens are issued with the public URL, but OIDC discovery returns internal URL
    public_issuer_url = settings.nextcloud_public_issuer_url
    client_issuer = public_issuer_url if public_issuer_url else issuer
    # Get MCP server URL for audience validation
    mcp_server_url = settings.nextcloud_mcp_server_url or _default_mcp_server_url()
    nextcloud_resource_uri = settings.nextcloud_resource_uri or nextcloud_host

    # Warn if resource URIs are not configured (required for ADR-005 compliance)
    if not settings.nextcloud_mcp_server_url:
        logger.warning(
            "NEXTCLOUD_MCP_SERVER_URL not set, defaulting to: %s. This should be set explicitly for proper audience validation.",
            mcp_server_url,
        )
    if not settings.nextcloud_resource_uri:
        logger.warning(
            "NEXTCLOUD_RESOURCE_URI not set, defaulting to: %s. This should be set explicitly for proper audience validation.",
            nextcloud_resource_uri,
        )

    # Create settings for UnifiedTokenVerifier (use same settings instance from start of function)
    # settings is already set at the start of setup_oauth_config()
    # Override with discovered values if not set in environment
    if not settings.oidc_client_id:
        settings.oidc_client_id = client_id
    if not settings.oidc_client_secret:
        settings.oidc_client_secret = client_secret
    if not settings.jwks_uri:
        settings.jwks_uri = jwks_uri
    if not settings.introspection_uri:
        settings.introspection_uri = introspection_uri
    if not settings.userinfo_uri:
        settings.userinfo_uri = userinfo_uri
    if not settings.oidc_issuer:
        # Use client_issuer which handles public URL override
        settings.oidc_issuer = client_issuer
    if not settings.nextcloud_mcp_server_url:
        settings.nextcloud_mcp_server_url = mcp_server_url
    if not settings.nextcloud_resource_uri:
        settings.nextcloud_resource_uri = nextcloud_resource_uri

    # Create Unified Token Verifier (ADR-005 compliant)
    token_verifier = UnifiedTokenVerifier(settings)

    # Log the mode
    logger.info(
        "✓ Multi-audience mode enabled (ADR-005) - tokens must contain both MCP and Nextcloud audiences"
    )
    logger.info("  Required MCP audience: %s or %s", client_id, mcp_server_url)
    logger.info("  Required Nextcloud audience: %s", nextcloud_resource_uri)

    if introspection_uri:
        logger.info("✓ Opaque token introspection enabled (RFC 7662)")
    if jwks_uri:
        logger.info("✓ JWT signature verification enabled (JWKS)")

    # Progressive Consent mode (for offline access / background jobs)
    encryption_key = settings.token_encryption_key
    if enable_offline_access and encryption_key and refresh_token_storage:
        logger.info("✓ Progressive Consent mode enabled - offline access available")

        # Note: Token Broker service would be initialized here for background job support
        # Currently not used in ADR-005 implementation as it's specific to offline access patterns
        # that are separate from the real-time token exchange flow
        logger.debug("Token broker available for future offline access features")

    oauth_client = None

    # Create auth settings
    mcp_server_url = settings.nextcloud_mcp_server_url or _default_mcp_server_url()

    # Note: We don't set required_scopes here anymore.
    # Scopes are now advertised via PRM endpoint and enforced per-tool.
    # This allows dynamic tool filtering based on user's actual token scopes.
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(
            client_issuer
        ),  # Use client issuer (may be public override)
        resource_server_url=AnyHttpUrl(mcp_server_url),
    )

    logger.info("OAuth configuration complete")

    return (
        nextcloud_host,
        token_verifier,
        auth_settings,
        refresh_token_storage,
        oauth_client,
        oauth_provider,
        client_id,
        client_secret,
    )


async def setup_oauth_config_for_multi_user_basic(
    settings: Settings,
    client_id: str,
    client_secret: str,
) -> tuple[UnifiedTokenVerifier, RefreshTokenStorage | None, str, str]:
    """
    Setup minimal OAuth configuration for multi-user BasicAuth mode.

    This is a lightweight version of setup_oauth_config() that:
    - Performs OIDC discovery to get endpoints
    - Creates UnifiedTokenVerifier for management API token validation
    - Creates RefreshTokenStorage for webhook token storage
    - Skips OAuth client creation (not needed for BasicAuth background sync)
    - Skips AuthSettings creation (not needed for BasicAuth MCP operations)

    This enables hybrid authentication mode where:
    - MCP operations use BasicAuth (stateless, simple)
    - Management APIs use OAuth bearer tokens (secure, per-user)
    - Background operations use OAuth refresh tokens (webhook sync)

    Args:
        settings: Application settings
        client_id: OAuth client ID (from DCR or static config)
        client_secret: OAuth client secret

    Returns:
        Tuple of (token_verifier, refresh_token_storage, client_id, client_secret)

    Raises:
        ValueError: If NEXTCLOUD_HOST is not set
        httpx.HTTPError: If OIDC discovery fails
    """
    nextcloud_host = settings.nextcloud_host
    if not nextcloud_host:
        raise ValueError("NEXTCLOUD_HOST is required for OAuth infrastructure setup")

    nextcloud_host = nextcloud_host.rstrip("/")

    # Get OIDC discovery URL (always Nextcloud integrated mode for multi-user BasicAuth)
    discovery_url = (
        settings.oidc_discovery_url
        or f"{nextcloud_host}/.well-known/openid-configuration"
    )
    logger.info(
        "Performing OIDC discovery for multi-user BasicAuth hybrid mode: %s",
        discovery_url,
    )

    # Perform OIDC discovery with the same cold-start retry/backoff as the
    # LOGIN_FLOW path (see _perform_oidc_discovery). This call degrades
    # gracefully at the caller rather than crashlooping, but without retry the
    # cold-start egress race would still disable hybrid-mode management APIs on
    # every pod restart until the next scan. The helper's exhaustion exceptions
    # (HTTPStatusError / RequestError / JSONDecodeError — the last a ValueError
    # subclass) all fall through to the handlers below.
    try:
        discovery = await _perform_oidc_discovery(discovery_url, settings, timeout=30.0)
    except httpx.HTTPStatusError as e:
        logger.error(
            "OIDC discovery failed: HTTP %s from %s",
            e.response.status_code,
            discovery_url,
        )
        raise ValueError(
            f"OIDC discovery failed: HTTP {e.response.status_code} from {discovery_url}. "
            "Ensure Nextcloud OIDC (user_oidc app) is installed and configured."
        ) from e
    except httpx.RequestError as e:
        logger.error("OIDC discovery failed: %s", e)
        raise ValueError(
            f"OIDC discovery failed: Cannot connect to {discovery_url}. Error: {e}"
        ) from e
    except (KeyError, ValueError) as e:
        logger.error(
            "OIDC discovery failed: Invalid response from %s: %s", discovery_url, e
        )
        raise ValueError(
            f"OIDC discovery failed: Invalid response from {discovery_url}. "
            "The endpoint did not return valid OIDC configuration."
        ) from e

    logger.info("✓ OIDC discovery successful (multi-user BasicAuth)")

    # Extract OIDC endpoints from discovery
    issuer = discovery["issuer"]
    userinfo_uri = discovery["userinfo_endpoint"]
    jwks_uri = discovery.get("jwks_uri")
    introspection_uri = discovery.get("introspection_endpoint")

    logger.info("OIDC endpoints configured for management API:")
    logger.info("  Issuer: %s", issuer)
    logger.info("  Userinfo: %s", userinfo_uri)
    logger.info("  JWKS: %s", jwks_uri)
    logger.info("  Introspection: %s", introspection_uri)

    # Get MCP server URL for audience validation
    mcp_server_url = settings.nextcloud_mcp_server_url or _default_mcp_server_url()
    nextcloud_resource_uri = settings.nextcloud_resource_uri or nextcloud_host

    # Use public issuer URL for JWT validation if set (handles Docker internal/external URL mismatch)
    # Tokens are issued with the public URL, but OIDC discovery returns internal URL
    public_issuer_url = settings.nextcloud_public_issuer_url
    client_issuer = public_issuer_url if public_issuer_url else issuer

    # Update settings with discovered values for UnifiedTokenVerifier
    if not settings.oidc_client_id:
        settings.oidc_client_id = client_id
    if not settings.oidc_client_secret:
        settings.oidc_client_secret = client_secret
    if not settings.jwks_uri:
        settings.jwks_uri = jwks_uri
    if not settings.introspection_uri:
        settings.introspection_uri = introspection_uri
    if not settings.userinfo_uri:
        settings.userinfo_uri = userinfo_uri
    if not settings.oidc_issuer:
        settings.oidc_issuer = client_issuer
    if not settings.nextcloud_mcp_server_url:
        settings.nextcloud_mcp_server_url = mcp_server_url
    if not settings.nextcloud_resource_uri:
        settings.nextcloud_resource_uri = nextcloud_resource_uri

    # Create Unified Token Verifier for management API authentication
    token_verifier = UnifiedTokenVerifier(settings)
    logger.info("✓ Token verifier created for management API (hybrid mode)")

    if introspection_uri:
        logger.info("  Opaque token introspection enabled (RFC 7662)")
    if jwks_uri:
        logger.info("  JWT signature verification enabled (JWKS)")

    # Initialize refresh token storage for background operations
    refresh_token_storage = None
    if settings.enable_offline_access:
        try:
            encryption_key = settings.token_encryption_key
            if not encryption_key:
                logger.warning(
                    "ENABLE_OFFLINE_ACCESS=true but TOKEN_ENCRYPTION_KEY not set. "
                    "Refresh tokens will NOT be stored. Generate a key with:\n"
                    '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
                )
            else:
                refresh_token_storage = RefreshTokenStorage.from_env()
                await refresh_token_storage.initialize()
                logger.info(
                    "✓ Refresh token storage initialized for background operations (hybrid mode)"
                )
        except Exception as e:
            logger.error("Failed to initialize refresh token storage: %s", e)
            logger.debug("Full traceback:\\n%s", traceback.format_exc())
            logger.warning(
                "Continuing without refresh token storage - webhook management may be limited"
            )

    logger.info(
        "OAuth infrastructure setup complete for multi-user BasicAuth hybrid mode"
    )

    return (token_verifier, refresh_token_storage, client_id, client_secret)


def get_app(transport: str = "streamable-http", enabled_apps: list[str] | None = None):
    # Initialize observability (logging will be configured by uvicorn)
    settings = get_settings()

    # Validate configuration and detect deployment mode
    mode, config_errors = validate_configuration(settings)

    if config_errors:
        error_msg = (
            f"Configuration validation failed for {mode.value} mode:\n"
            + "\n".join(f"  - {err}" for err in config_errors)
            + "\n\n"
            + get_mode_summary(mode)
        )
        logger.error(error_msg)
        raise ValueError(error_msg)

    logger.info("✅ Configuration validated successfully for %s mode", mode.value)
    logger.debug("Mode details:\\n%s", get_mode_summary(mode))

    # Derive helper variables for backward compatibility with existing code.
    # `oauth_enabled` is True for the LOGIN_FLOW (formerly OAUTH_SINGLE_AUDIENCE)
    # multi-user OAuth mode — in this mode the MCP server is an OIDC relying
    # party and Login Flow v2 acquires per-user Nextcloud app passwords.
    oauth_enabled = mode == AuthMode.LOGIN_FLOW
    # Log hybrid authentication status for multi-user BasicAuth with offline access
    if mode == AuthMode.MULTI_USER_BASIC and settings.enable_offline_access:
        logger.info(
            "🔄 Hybrid authentication mode will be enabled:\n"
            "  - MCP operations: BasicAuth (stateless, credentials per-request)\n"
            "  - Management APIs: OAuth bearer tokens (secure, per-user)\n"
            "  - Background operations: OAuth refresh tokens (webhook sync)"
        )

    # Setup Prometheus metrics (always enabled by default)
    if settings.metrics_enabled:
        setup_metrics(port=settings.metrics_port)
        logger.info(
            "Prometheus metrics enabled on dedicated port %s", settings.metrics_port
        )

    # Setup OpenTelemetry tracing (optional)
    if settings.otel_exporter_otlp_endpoint:
        setup_tracing(
            service_name=settings.otel_service_name,
            otlp_endpoint=settings.otel_exporter_otlp_endpoint,
            otlp_verify_ssl=settings.otel_exporter_verify_ssl,
            sampling_rate=settings.otel_traces_sampler_arg,
        )
        logger.info(
            "OpenTelemetry tracing enabled (endpoint: %s)",
            settings.otel_exporter_otlp_endpoint,
        )
    else:
        logger.info(
            "OpenTelemetry tracing disabled (set OTEL_EXPORTER_OTLP_ENDPOINT to enable)"
        )

    # Initialize OAuth credentials for multi-user modes that need background operations
    # This must happen BEFORE uvicorn starts (same lifecycle point as OAuth modes)
    # to avoid async context issues
    multi_user_basic_oauth_creds: tuple[str, str] | None = None
    multi_user_token_verifier: UnifiedTokenVerifier | None = None
    multi_user_refresh_storage: RefreshTokenStorage | None = None

    if (
        mode == AuthMode.MULTI_USER_BASIC
        and settings.vector_sync_enabled
        and settings.enable_background_operations
    ):
        logger.info(
            "Multi-user BasicAuth with vector sync - checking for OAuth/app password credentials"
        )

        # Check for static credentials first
        static_client_id = settings.oidc_client_id
        static_client_secret = settings.oidc_client_secret

        if static_client_id and static_client_secret:
            logger.info("Using static OAuth credentials for background operations")
            multi_user_basic_oauth_creds = (static_client_id, static_client_secret)
        else:
            # Perform DCR before uvicorn starts (same lifecycle as OAuth modes)
            logger.info(
                "OAuth credentials not configured - attempting Dynamic Client Registration..."
            )

            async def setup_multi_user_basic_dcr():
                """Setup DCR for multi-user BasicAuth background operations."""
                # Construct registration endpoint directly from nextcloud_host
                # Standard RFC 7591 endpoint pattern for Nextcloud OIDC
                # This avoids relying on discovery doc which may use public URLs unreachable from containers
                registration_endpoint = f"{settings.nextcloud_host}/apps/oidc/register"
                logger.info(
                    "Attempting Dynamic Client Registration at: %s",
                    registration_endpoint,
                )

                # Perform DCR
                try:
                    # Assert nextcloud_host is not None (required for multi-user mode)
                    assert settings.nextcloud_host is not None, (
                        "NEXTCLOUD_HOST is required"
                    )

                    client_id, client_secret = await load_oauth_client_credentials(
                        nextcloud_host=settings.nextcloud_host,
                        registration_endpoint=registration_endpoint,
                    )
                    logger.info(
                        "✓ Dynamic Client Registration successful for background operations (client_id: %s...)",
                        client_id[:16],
                    )
                    return (client_id, client_secret)
                except Exception as e:
                    logger.error("Dynamic Client Registration failed: %s", e)
                    logger.debug("Full traceback:\\n%s", traceback.format_exc())
                    logger.warning("Background vector sync will be disabled.")
                    return None

            # Run DCR synchronously before uvicorn starts
            multi_user_basic_oauth_creds = anyio.run(setup_multi_user_basic_dcr)

        # Setup OAuth infrastructure for management APIs and background operations
        # This creates the UnifiedTokenVerifier needed by management.py and
        # RefreshTokenStorage for webhook token persistence
        if multi_user_basic_oauth_creds:
            sync_client_id, sync_client_secret = multi_user_basic_oauth_creds

            logger.info(
                "Setting up OAuth infrastructure for management APIs (hybrid mode)..."
            )

            try:
                (
                    multi_user_token_verifier,
                    multi_user_refresh_storage,
                    _,
                    _,
                ) = anyio.run(
                    setup_oauth_config_for_multi_user_basic,
                    settings,
                    sync_client_id,
                    sync_client_secret,
                )
                logger.info(
                    "✓ OAuth infrastructure setup complete for multi-user BasicAuth hybrid mode"
                )
            except (httpx.HTTPError, ValueError, KeyError) as e:
                # Expected errors during OAuth infrastructure setup:
                # - httpx.HTTPError: Network issues, OIDC discovery failures
                # - ValueError: Missing required configuration (NEXTCLOUD_HOST)
                # - KeyError: Missing required fields in OIDC discovery response
                logger.error("Failed to setup OAuth infrastructure: %s", e)
                logger.debug("Full traceback:\\n%s", traceback.format_exc())
                logger.warning(
                    "Management API will be unavailable. "
                    "Webhook management from Astrolabe admin UI will not work."
                )
                # Set to None to indicate failure
                multi_user_token_verifier = None
                multi_user_refresh_storage = None
            except Exception as e:
                # Unexpected error - this is a programming error, re-raise it
                logger.error(
                    "Unexpected error during OAuth infrastructure setup: %s. This is likely a programming error that should be fixed.",
                    e,
                )
                raise

    # Create MCP server based on detected mode
    if mode == AuthMode.LOGIN_FLOW:
        logger.info("Configuring MCP server for %s mode", mode.value)
        # Asynchronously get the OAuth configuration

        (
            nextcloud_host,
            token_verifier,
            auth_settings,
            refresh_token_storage,
            oauth_client,
            oauth_provider,
            client_id,
            client_secret,
        ) = anyio.run(setup_oauth_config)

        # Create lifespan function with captured OAuth context (closure)
        @asynccontextmanager
        async def oauth_lifespan(server: FastMCP) -> AsyncIterator[OAuthAppContext]:
            """
            Lifespan context for OAuth mode - captures OAuth configuration from outer scope.
            """
            logger.info("Starting MCP server in OAuth mode")
            logger.info("Using OAuth provider: %s", oauth_provider)
            if refresh_token_storage:
                logger.info("Refresh token storage is available")
            if oauth_client:
                logger.info("OAuth client is available for token refresh")

            # Initialize document processors
            initialize_document_processors()

            try:
                yield OAuthAppContext(
                    nextcloud_host=nextcloud_host,
                    token_verifier=token_verifier,
                    refresh_token_storage=refresh_token_storage,
                    oauth_client=oauth_client,
                    oauth_provider=oauth_provider,
                    server_client_id=client_id,
                    document_send_stream=_vector_sync_state.document_send_stream,
                    document_receive_stream=_vector_sync_state.document_receive_stream,
                    shutdown_event=_vector_sync_state.shutdown_event,
                    scanner_wake_event=_vector_sync_state.scanner_wake_event,
                    # task_producer and eviction_task_group are exposed via
                    # @property (read _vector_sync_state at access time).
                )
            finally:
                logger.info("Shutting down MCP server")
                # Dispose the RefreshTokenStorage engine so pooled
                # asyncpg connections drain cleanly on SIGTERM instead
                # of leaking server-side slots until the Postgres
                # idle-timeout fires (ADR-026, PR #798 round-4).
                if refresh_token_storage is not None:
                    try:
                        await refresh_token_storage.close()
                    except Exception as e:
                        logger.warning("Error disposing refresh-token storage: %s", e)
                # OAuth client cleanup (if it has a close method)
                if oauth_client and hasattr(oauth_client, "close"):
                    try:
                        await oauth_client.close()
                    except Exception as e:
                        logger.warning("Error closing OAuth client: %s", e)
                logger.info("MCP server shutdown complete")

        mcp = FastMCP(
            "Nextcloud MCP",
            lifespan=oauth_lifespan,
            token_verifier=token_verifier,
            auth=auth_settings,
            # Disable DNS rebinding protection for containerized deployments (k8s, Docker)
            # MCP 1.23+ auto-enables this for localhost, breaking k8s service DNS names
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
    else:
        # BasicAuth modes (single-user or multi-user)
        logger.info("Configuring MCP server for %s mode", mode.value)
        mcp = FastMCP(
            "Nextcloud MCP",
            lifespan=app_lifespan_basic,
            # Disable DNS rebinding protection for containerized deployments (k8s, Docker)
            # MCP 1.23+ auto-enables this for localhost, breaking k8s service DNS names
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )

    @mcp.resource("nc://capabilities")
    async def nc_get_capabilities():
        """Get the Nextcloud Host capabilities"""
        ctx: Context = mcp.get_context()
        client = await get_nextcloud_client(ctx)
        return await client.capabilities()

    # If no specific apps are specified, enable all
    if enabled_apps is None:
        enabled_apps = list(AVAILABLE_APPS.keys())

    # Configure only the enabled apps
    for app_name in enabled_apps:
        if app_name in AVAILABLE_APPS:
            logger.info("Configuring %s tools", app_name)
            AVAILABLE_APPS[app_name](mcp)
        else:
            logger.warning(
                "Unknown app: %s. Available apps: %s",
                app_name,
                list(AVAILABLE_APPS.keys()),
            )

    # Register semantic search tools (cross-app feature)
    if settings.vector_sync_enabled:
        if settings.dense_enabled:
            logger.info(
                "Configuring search tools (vector sync enabled, SEARCH_MODE=hybrid)"
            )
        else:
            logger.info(
                "Configuring search tools (vector sync enabled, SEARCH_MODE=keyword):"
                " dense embeddings disabled; BM25 keyword search only — no embedding"
                " endpoint required (ADR-030)"
            )
        configure_semantic_tools(mcp)
    else:
        logger.info("Skipping semantic search tools (VECTOR_SYNC_ENABLED not set)")
        if not settings.dense_enabled:
            # keyword mode is meaningless without the Qdrant pipeline it queries;
            # the tools just won't register. Warn rather than crash, matching the
            # codebase's gate-don't-crash posture for search enablement.
            logger.warning(
                "SEARCH_MODE=keyword has no effect while VECTOR_SYNC_ENABLED is "
                "off: keyword search uses the Qdrant index, so enable vector sync "
                "to use it (ADR-030)"
            )

    # Register OAuth provisioning tools (only when offline access is enabled)
    enable_offline_access_for_tools = settings.enable_offline_access
    if oauth_enabled and enable_offline_access_for_tools:
        logger.info("Registering OAuth provisioning tools for offline access")
        register_oauth_tools(mcp)
    elif oauth_enabled and not enable_offline_access_for_tools:
        logger.info(
            "Skipping provisioning tools registration (offline access not enabled)"
        )

    # Register Login Flow v2 auth tools (ADR-022)
    if settings.enable_login_flow:
        logger.info("Registering Login Flow v2 auth tools")
        register_auth_tools(mcp)

    # Override list_tools to filter based on user's token scopes (OAuth mode only)
    if oauth_enabled:
        original_list_tools = mcp._tool_manager.list_tools

        def list_tools_filtered():
            """List tools filtered by user's token scopes (JWT and Bearer tokens)."""
            # Get user's scopes from token using MCP SDK's contextvar
            # This works for all request types including list_tools
            user_scopes = get_access_token_scopes()
            is_jwt = is_jwt_token()
            logger.info(
                "🔍 list_tools called - Token type: %s, User scopes: %s",
                "JWT" if is_jwt else "opaque/none",
                user_scopes,
            )

            # Get all tools
            all_tools = original_list_tools()

            # Filter tools based on user's token scopes (both JWT and opaque tokens)
            # JWT tokens have scopes embedded in payload
            # Opaque tokens get scopes via introspection endpoint
            # Claude Code now properly respects PRM endpoint for scope discovery
            if user_scopes:
                allowed_tools = [
                    tool
                    for tool in all_tools
                    if has_required_scopes(tool.fn, user_scopes)
                ]
                token_type = "JWT" if is_jwt else "Bearer"
                logger.info(
                    "✂️ %s scope filtering: %s/%s tools available for scopes: %s",
                    token_type,
                    len(allowed_tools),
                    len(all_tools),
                    user_scopes,
                )
            else:
                # BasicAuth mode or no token - show all tools
                allowed_tools = all_tools
                logger.info(
                    "📋 Showing all %s tools (no token/BasicAuth)", len(all_tools)
                )

            # Return the Tool objects directly (they're already in the correct format)
            return allowed_tools

        # Replace the tool manager's list_tools method
        mcp._tool_manager.list_tools = list_tools_filtered  # type: ignore[method-assign]
        logger.info(
            "Dynamic tool filtering enabled for OAuth mode (JWT and Bearer tokens)"
        )

    mcp_app = mcp.streamable_http_app()

    async def _login_flow_cleanup_loop() -> None:
        """Periodically clean up expired Login Flow v2 sessions and proxy codes."""
        from nextcloud_mcp_server.auth.oauth_routes import (  # noqa: PLC0415
            _cleanup_expired_proxy_codes,
        )
        from nextcloud_mcp_server.auth.provision_routes import (  # noqa: PLC0415
            _cleanup_expired_sessions as _cleanup_expired_provision_sessions,
        )

        while True:
            try:
                storage = await get_shared_storage()
                count = await storage.delete_expired_login_flow_sessions()
                if count:
                    logger.info("Cleaned up %s expired login flow sessions", count)
                # Browser session rows are otherwise only cleaned up lazily
                # when a user revisits — PR #758 finding 6.
                await storage.cleanup_expired_browser_sessions()
                # Also clean up expired AS proxy codes/sessions
                _cleanup_expired_proxy_codes()
                # Clean up expired web provision sessions
                _cleanup_expired_provision_sessions()
            except Exception as e:
                logger.warning("Login flow cleanup error: %s", e)
            await anyio.sleep(3600)  # Every hour

    @asynccontextmanager
    async def _maybe_login_flow_cleanup(app: Starlette):
        """Start Login Flow cleanup task and provision poll task group.

        The task group is always created (even when Login Flow cleanup is
        disabled) because provision routes use it to spawn background poll
        tasks via ``browser_app.state.poll_task_group``.
        """
        async with anyio.create_task_group() as tg:
            if settings.enable_login_flow:
                tg.start_soon(_login_flow_cleanup_loop)
            # Share task group with provision routes for background polling
            found_app_mount = False
            for route in app.routes:
                if isinstance(route, Mount) and route.path == "/app":
                    browser_app = cast(Starlette, route.app)
                    browser_app.state.poll_task_group = tg
                    found_app_mount = True
                    break
            if not found_app_mount:
                logger.warning(
                    "Could not find /app mount to share poll task group; "
                    "web provisioning will return 500"
                )
            yield
            tg.cancel_scope.cancel()

    @asynccontextmanager
    async def _mcp_session_with_login_flow(app: Starlette):
        """Start MCP session manager with optional Login Flow cleanup."""
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp.session_manager.run())
            await stack.enter_async_context(_maybe_login_flow_cleanup(app))
            yield

    async def _sweep_orphan_placeholders_if_enabled() -> None:
        """One-shot Pod-startup sweep of cross-restart placeholder orphans.

        See ``vector.placeholder.sweep_orphan_placeholders`` and Deck
        card #101. Both lifespan branches (single-user BasicAuth and
        OAuth / multi-user BasicAuth) call this after the qdrant
        client is initialised and before the scanner / user-manager
        tasks spawn. Failures are non-fatal — the existing staleness
        gate will eventually re-queue orphans on the slow ~5h path.
        """
        if not settings.vector_sync_orphan_sweep_enabled:
            return
        try:
            qdrant_client = await get_qdrant_client()
            collection = settings.get_collection_name()
            swept, kept = await sweep_orphan_placeholders(qdrant_client, collection)
            logger.info(
                "vector_sync.orphan_sweep",
                extra={
                    "swept": swept,
                    "kept": kept,
                    "collection": collection,
                },
            )
        except Exception:
            logger.exception("vector_sync.orphan_sweep_failed")

    @asynccontextmanager
    async def starlette_lifespan(app: Starlette):
        # Set OAuth context for OAuth login routes (ADR-004)
        if oauth_enabled:
            # Prepare OAuth config from setup_oauth_config closure variables
            # Get nextcloud_host from settings (it was validated as required)
            nextcloud_host_for_context = settings.nextcloud_host
            if not nextcloud_host_for_context:
                raise ValueError("NEXTCLOUD_HOST is required for OAuth mode")

            mcp_server_url = (
                settings.nextcloud_mcp_server_url or _default_mcp_server_url()
            )
            nextcloud_resource_uri = (
                settings.nextcloud_resource_uri or nextcloud_host_for_context
            )
            discovery_url = (
                settings.oidc_discovery_url
                or f"{nextcloud_host_for_context}/.well-known/openid-configuration"
            )
            scopes = settings.oidc_scopes

            oauth_context_dict = {
                "storage": refresh_token_storage,
                "oauth_client": oauth_client,
                "token_verifier": token_verifier,  # For querying IdP userinfo endpoint
                "config": {
                    "mcp_server_url": mcp_server_url,
                    "discovery_url": discovery_url,
                    "client_id": client_id,  # From setup_oauth_config (DCR or static)
                    "client_secret": client_secret,  # From setup_oauth_config (DCR or static)
                    "scopes": scopes,
                    "nextcloud_host": nextcloud_host_for_context,
                    "nextcloud_resource_uri": nextcloud_resource_uri,
                    "oauth_provider": oauth_provider,
                },
            }
            app.state.oauth_context = oauth_context_dict

            # Also set oauth_context on browser_app for session authentication
            # browser_app is in the same function scope (defined later in create_app)
            # We need to find it in the mounted routes
            for route in app.routes:
                if isinstance(route, Mount) and route.path == "/app":
                    browser_app = cast(Starlette, route.app)
                    browser_app.state.oauth_context = oauth_context_dict
                    logger.info(
                        "OAuth context shared with browser_app for session auth"
                    )
                    break

            logger.info(
                "OAuth context initialized for login routes (client_id=%s...)",
                client_id[:16],
            )
        else:
            # BasicAuth mode - initialize storage for webhook management
            basic_auth_storage = RefreshTokenStorage.from_env()
            await basic_auth_storage.initialize()
            logger.info("Initialized refresh token storage for webhook management")

            app.state.storage = basic_auth_storage

            # For multi-user BasicAuth with offline access, create oauth_context for management APIs
            # This allows Astrolabe to use management APIs with OAuth bearer tokens
            if settings.enable_multi_user_basic_auth and settings.enable_offline_access:
                # Check if we have OAuth credentials AND infrastructure from setup
                if (
                    multi_user_basic_oauth_creds
                    and multi_user_token_verifier is not None
                ):
                    sync_client_id, sync_client_secret = multi_user_basic_oauth_creds

                    # Create oauth_context for management API authentication
                    nextcloud_host_for_context = settings.nextcloud_host
                    mcp_server_url = (
                        settings.nextcloud_mcp_server_url or _default_mcp_server_url()
                    )
                    discovery_url = (
                        settings.oidc_discovery_url
                        or f"{nextcloud_host_for_context}/.well-known/openid-configuration"
                    )

                    oauth_context_dict = {
                        # Use OAuth refresh token storage if available, fallback to basic_auth_storage
                        "storage": multi_user_refresh_storage or basic_auth_storage,
                        "oauth_client": None,  # Not needed for management APIs
                        "token_verifier": multi_user_token_verifier,  # FIXED: Now has real verifier!
                        "config": {
                            "mcp_server_url": mcp_server_url,
                            "discovery_url": discovery_url,
                            "client_id": sync_client_id,
                            "client_secret": sync_client_secret,
                            "scopes": "",  # Background sync only
                            "nextcloud_host": nextcloud_host_for_context,
                            "nextcloud_resource_uri": nextcloud_host_for_context,
                            "oauth_provider": "nextcloud",  # Always Nextcloud for multi-user BasicAuth
                        },
                    }
                    app.state.oauth_context = oauth_context_dict
                    logger.info(
                        "✓ OAuth context initialized for management APIs (hybrid mode, client_id=%s...)",
                        sync_client_id[:16],
                    )
                elif multi_user_basic_oauth_creds and multi_user_token_verifier is None:
                    logger.warning(
                        "OAuth infrastructure setup failed - management API will be unavailable. "
                        "This is expected if OIDC discovery failed or token verifier creation failed. "
                        "Webhook management from Astrolabe admin UI will not work."
                    )
                else:
                    logger.warning(
                        "OAuth credentials not available - management API will be unavailable. "
                        "This is expected if DCR failed or static credentials were not provided. "
                        "Webhook management from Astrolabe admin UI will not work."
                    )

            # Also share with browser_app for webhook routes
            for route in app.routes:
                if isinstance(route, Mount) and route.path == "/app":
                    browser_app = cast(Starlette, route.app)
                    browser_app.state.storage = basic_auth_storage
                    if (
                        settings.enable_multi_user_basic_auth
                        and settings.enable_offline_access
                        and hasattr(app.state, "oauth_context")
                    ):
                        browser_app.state.oauth_context = app.state.oauth_context
                        logger.info(
                            "OAuth context shared with browser_app for management APIs"
                        )
                    logger.info(
                        "Storage shared with browser_app for webhook management"
                    )
                    break

        # Start background vector sync tasks (ADR-007)
        # Scanner runs at server-level (once), not per-session

        # Re-use settings from outer scope (already validated)
        # Note: enable_offline_access_for_sync, encryption_key, and refresh_token_storage
        # are already defined in outer scope before mode split

        # Each deployment mode contributes its background-sync work as a
        # (start, teardown) pair; the shared task group further below runs that
        # work alongside the readiness health-refresh loop and yields once
        # through the MCP session manager — collapsing four near-identical
        # task-group + session + yield + teardown skeletons into one.
        async def _noop_start(tg: TaskGroup) -> None:
            """No background sync tasks for this mode."""

        async def _noop_teardown() -> None:
            """No mode-owned resources to release."""

        start, teardown = _noop_start, _noop_teardown

        # Multi-user BasicAuth uses OAuth-style background sync (with app
        # passwords); single-user BasicAuth sync is skipped in multi-user mode.
        if (
            settings.vector_sync_enabled
            and not oauth_enabled
            and not settings.enable_multi_user_basic_auth
        ):
            # BasicAuth mode - single user sync
            logger.info("Starting background vector sync tasks for BasicAuth mode")

            # Get username from settings
            username = settings.nextcloud_username
            if not username:
                raise ValueError(
                    "NEXTCLOUD_USERNAME required for vector sync in BasicAuth mode"
                )

            # Create client for vector sync (server-level, not per-session)
            client = NextcloudClient.from_env()

            # Initialize Qdrant collection before starting background tasks
            logger.info("Initializing Qdrant collection...")

            try:
                await get_qdrant_client()  # Triggers collection creation if needed
                logger.info("Qdrant collection ready")
            except Exception as e:
                logger.error("Failed to initialize Qdrant collection: %s", e)
                raise RuntimeError(
                    f"Cannot start vector sync - Qdrant initialization failed: {e}"
                ) from e

            # Orphan-sweep before scanner starts — card #101.
            await _sweep_orphan_placeholders_if_enabled()

            # Initialize the ingest transport. INGEST_QUEUE selects the backend
            # (Deck #183, ADR-028): ``memory`` builds an in-process anyio stream
            # drained by an in-process pool (SQLite/dev); ``postgres`` defers jobs
            # to the per-tenant Postgres via procrastinate and runs no in-process
            # consumer (the separate ``worker`` role drains the queue). The
            # transport hides that choice — this path is now backend-agnostic.
            shutdown_event = anyio.Event()
            scanner_wake_event = anyio.Event()

            # Named ingest_transport (not transport) to avoid shadowing the
            # get_app(transport=...) HTTP-transport parameter.
            ingest_transport = await build_transport(settings)

            # Publish to app.state (ADR-007), the module singleton (FastMCP
            # session lifespans), and the /app browser sub-app in one place.
            _wire_vector_sync_state(
                app, ingest_transport, shutdown_event, scanner_wake_event
            )

            # Background-sync work for this mode; the shared runner starts it.
            async def _single_user_start(tg: TaskGroup) -> None:
                # Scanner publishes to the transport's producer.
                await tg.start(
                    scanner_task,
                    ingest_transport.producer,
                    shutdown_event,
                    scanner_wake_event,
                    client,
                    username,
                )

                # In-process consumer pool. ``run_consumers`` is a no-op for the
                # distributed (postgres) backend — the out-of-process ``worker``
                # role consumes there. The closure binds this mode's shared
                # client+username and forwards anyio's injected ``task_status``.
                # One shared receive stream + N workers ⇒ a single multiplexed
                # queue processed with N-way parallelism (ADR-028).
                async def spawn_worker(
                    worker_id, receive_stream, *, task_status=anyio.TASK_STATUS_IGNORED
                ):
                    await processor_task(
                        worker_id,
                        receive_stream,
                        shutdown_event,
                        client,
                        username,
                        task_status=task_status,
                    )

                await ingest_transport.run_consumers(
                    tg, spawn_worker, settings.vector_sync_processor_workers
                )

                # Outstanding-work + corpus gauges on a fixed cadence,
                # independent of the consumer path and queue backend (fixes the
                # gauge reading 0 on the multi-user path; see metrics_publisher).
                # receive_stream is None in postgres mode — get_ingest_pending
                # falls back to the procrastinate job counts there.
                await tg.start(
                    vector_sync_metrics_task,
                    ingest_transport.producer,
                    ingest_transport.receive_stream,
                    shutdown_event,
                )

                logger.info(
                    "Background sync tasks started: 1 scanner + %s processors (queue=%s)",
                    ingest_transport.active_consumer_count,
                    ingest_transport.backend_name,
                )

            async def _single_user_teardown() -> None:
                shutdown_event.set()
                # Tear down backend-owned resources (closes the procrastinate
                # connector pool in postgres mode; no-op for the memory stream,
                # which task-group cancellation closes).
                await ingest_transport.aclose()
                # Drop stale singleton refs to the now-closed transport.
                _clear_vector_sync_state()
                await client.close()

            start, teardown = _single_user_start, _single_user_teardown

        elif (
            settings.vector_sync_enabled
            and (oauth_enabled or settings.enable_multi_user_basic_auth)
            and settings.enable_background_operations
        ):
            # OAuth mode with background operations - multi-user sync
            # Also used for multi-user BasicAuth mode (client auth is BasicAuth, background sync uses app passwords or OAuth)
            mode_desc = "OAuth mode" if oauth_enabled else "Multi-user BasicAuth mode"
            logger.info("Starting background vector sync tasks for %s", mode_desc)

            # Get nextcloud_host (from settings - already validated)
            nextcloud_host_for_sync = settings.nextcloud_host
            if not nextcloud_host_for_sync:
                raise ValueError("NEXTCLOUD_HOST required for vector sync")

            # Get OIDC discovery URL (same as used for OAuth setup)
            discovery_url = (
                settings.oidc_discovery_url
                or f"{nextcloud_host_for_sync}/.well-known/openid-configuration"
            )

            # Get client credentials - these were obtained before uvicorn started
            # For OAuth modes: from setup_oauth_config()
            # For multi-user BasicAuth: from setup_multi_user_basic_dcr()
            oauth_ctx = getattr(app.state, "oauth_context", {})
            oauth_config = oauth_ctx.get("config", {})
            sync_client_id = oauth_config.get("client_id")
            sync_client_secret = oauth_config.get("client_secret")

            # For multi-user BasicAuth mode, use pre-obtained credentials from outer scope
            if not sync_client_id or not sync_client_secret:
                if multi_user_basic_oauth_creds:
                    sync_client_id, sync_client_secret = multi_user_basic_oauth_creds
                    logger.info(
                        "Using pre-obtained OAuth credentials for background sync"
                    )
                else:
                    # No credentials available - DCR was attempted before uvicorn started but failed
                    sync_client_id = None
                    sync_client_secret = None
                    logger.warning(
                        "OAuth credentials not available for background sync "
                        "(DCR was attempted during startup but failed)"
                    )

            # Only start vector sync if credentials are available
            if sync_client_id and sync_client_secret:
                # Get storage - different for OAuth vs multi-user BasicAuth modes
                # OAuth mode: refresh_token_storage (from setup_oauth_config)
                # Multi-user BasicAuth: app.state.storage (basic_auth_storage)
                token_storage = (
                    refresh_token_storage if oauth_enabled else app.state.storage
                )

                # Create token broker for background operations
                # Note: storage handles encryption internally, no key needed here
                # Client credentials are needed for token refresh operations
                token_broker = TokenBrokerService(
                    storage=token_storage,
                    oidc_discovery_url=discovery_url,
                    nextcloud_host=nextcloud_host_for_sync,
                    client_id=sync_client_id,
                    client_secret=sync_client_secret,
                )

                # Store token broker in oauth_context for management API (revoke endpoint)
                if hasattr(app.state, "oauth_context"):
                    app.state.oauth_context["token_broker"] = token_broker
                    logger.info(
                        "Token broker added to oauth_context for management API"
                    )

                # Initialize Qdrant collection before starting background tasks
                logger.info("Initializing Qdrant collection...")

                try:
                    await get_qdrant_client()  # Triggers collection creation if needed
                    logger.info("Qdrant collection ready")
                except Exception as e:
                    logger.error("Failed to initialize Qdrant collection: %s", e)
                    raise RuntimeError(
                        f"Cannot start vector sync - Qdrant initialization failed: {e}"
                    ) from e

                # Orphan-sweep before scanners spawn — card #101. Runs once
                # across the shared (per-tenant) collection regardless of
                # how many per-user scanners the user-manager later starts.
                await _sweep_orphan_placeholders_if_enabled()

                # Clean up stale app passwords at startup. All deployment modes
                # now authenticate background sync via locally-stored app
                # passwords (the OAuth refresh-token path was removed), so this
                # must run regardless of oauth_enabled — login_flow tenants were
                # previously skipped, letting deleted-user credentials linger and
                # drive an endless scanner re-spawn/401 loop (Deck #198). The
                # credential_cleanup_task started below repeats it on a cadence.
                try:
                    # Log the cohort first: the sweep makes one OCS validation
                    # call per stored user before readiness, so the count is the
                    # operability signal if startup latency ever climbs.
                    stored = await token_storage.get_all_app_password_user_ids()
                    if stored:
                        logger.info(
                            "Running startup credential sweep for %s stored user(s)",
                            len(stored),
                        )
                    removed = await token_storage.cleanup_invalid_app_passwords(
                        nextcloud_host=nextcloud_host_for_sync
                    )
                    if removed:
                        logger.info(
                            "Cleaned up %s stale app password(s): %s",
                            len(removed),
                            removed,
                        )
                except Exception as e:
                    logger.warning("App password cleanup failed (non-fatal): %s", e)

                # Initialize the ingest transport. INGEST_QUEUE selects the
                # backend (Deck #183, ADR-028): ``memory`` builds an in-process
                # anyio stream drained by an in-process pool; ``postgres`` defers
                # jobs via procrastinate and runs no in-process consumer (the
                # separate ``worker`` role drains the queue). The transport hides
                # that choice — this path is now backend-agnostic.
                shutdown_event = anyio.Event()
                scanner_wake_event = anyio.Event()
                # Doorbell the provisioning request path rings (via
                # notify_user_provisioned) to wake the user manager immediately
                # for a newly provisioned user. Held on the singleton only — both
                # the manager and the signal helper reach it there.
                provision_signal = ProvisionSignal()
                _vector_sync_state.provision_signal = provision_signal

                # User state tracking for user manager
                user_states: dict = {}

                # Named ingest_transport (not transport) to avoid shadowing the
                # get_app(transport=...) HTTP-transport parameter.
                ingest_transport = await build_transport(settings)

                # Publish to app.state (ADR-007), the module singleton (FastMCP
                # session lifespans), and the /app browser sub-app in one place.
                _wire_vector_sync_state(
                    app, ingest_transport, shutdown_event, scanner_wake_event
                )

                # Background sync authenticates as each provisioned user via
                # locally-stored Nextcloud app passwords (Login Flow v2 /
                # multi-user BasicAuth). The earlier OAuth refresh-token
                # path in vector/oauth_sync.py was removed in the ADR-022
                # cleanup — it relied on unmerged user_oidc patches and was
                # never reachable from any supported deployment mode. The
                # `token_broker` constructed above is still used by the
                # management API revoke endpoint (via app.state.oauth_context).
                async def _multi_user_start(tg: TaskGroup) -> None:
                    # User manager supervises per-user scanners. Each per-user
                    # scanner clones the producer; for the bus producer clone()
                    # returns the shared connection.
                    await tg.start(
                        user_manager_task,
                        ingest_transport.producer,
                        shutdown_event,
                        scanner_wake_event,
                        token_storage,
                        nextcloud_host_for_sync,
                        user_states,
                        tg,
                        provision_signal,
                    )

                    # Periodic backstop sweep removing app passwords that no
                    # longer authenticate (deleted/disabled users), complementing
                    # the per-scanner self-heal in user_scanner_task (Deck #198).
                    await tg.start(
                        credential_cleanup_task,
                        token_storage,
                        shutdown_event,
                        nextcloud_host_for_sync,
                    )

                    # In-process consumer pool. ``run_consumers`` is a no-op for
                    # the distributed (postgres) backend — the out-of-process
                    # ``worker`` role consumes there. The closure binds this
                    # mode's nextcloud_host (per-document credential resolution)
                    # and forwards anyio's injected ``task_status``. One shared
                    # receive stream + N workers ⇒ a single multiplexed queue
                    # draining every user's documents with N-way parallelism,
                    # never one user at a time (ADR-028).
                    async def spawn_worker(
                        worker_id,
                        receive_stream,
                        *,
                        task_status=anyio.TASK_STATUS_IGNORED,
                    ):
                        await oauth_processor_task(
                            worker_id,
                            receive_stream,
                            shutdown_event,
                            nextcloud_host_for_sync,
                            task_status=task_status,
                        )

                    await ingest_transport.run_consumers(
                        tg, spawn_worker, settings.vector_sync_processor_workers
                    )

                    # Outstanding-work + corpus gauges on a fixed cadence.
                    # Critical on this multi-user path: the consumer is
                    # oauth_processor_task, which never updated the queue gauge,
                    # so without this the gauge read 0 while the buffer held
                    # thousands of pending docs (see metrics_publisher).
                    # receive_stream is None in postgres mode — get_ingest_pending
                    # falls back to the procrastinate job counts there.
                    await tg.start(
                        vector_sync_metrics_task,
                        ingest_transport.producer,
                        ingest_transport.receive_stream,
                        shutdown_event,
                    )

                    logger.info(
                        "Background sync tasks started: 1 user manager + %s processors (queue=%s)",
                        ingest_transport.active_consumer_count,
                        ingest_transport.backend_name,
                    )

                async def _multi_user_teardown() -> None:
                    shutdown_event.set()
                    # Tear down backend-owned resources (closes the procrastinate
                    # connector pool in postgres mode; no-op for the memory stream).
                    await ingest_transport.aclose()
                    # Drop stale singleton refs to the now-closed transport.
                    _clear_vector_sync_state()
                    # Close token broker HTTP client
                    if token_broker._http_client:
                        await token_broker._http_client.aclose()

                start, teardown = _multi_user_start, _multi_user_teardown
            else:
                # No OAuth credentials available for background sync
                logger.warning(
                    "Skipping background vector sync - OAuth credentials not available. "
                    "Multi-user BasicAuth mode will run without semantic search background operations. "
                    "To enable, set NEXTCLOUD_OIDC_CLIENT_ID and NEXTCLOUD_OIDC_CLIENT_SECRET."
                )
                # start/teardown stay no-op; the shared runner yields below.

        else:
            # No vector sync - just run MCP session manager
            if settings.vector_sync_enabled:
                # Log why vector sync is not starting
                if oauth_enabled and not settings.enable_offline_access:
                    logger.warning(
                        "Vector sync enabled but ENABLE_OFFLINE_ACCESS=false - "
                        "vector sync requires offline access in OAuth mode"
                    )
                elif oauth_enabled and not refresh_token_storage:
                    logger.warning(
                        "Vector sync enabled but refresh token storage not available"
                    )
                elif oauth_enabled and not settings.token_encryption_key:
                    logger.warning(
                        "Vector sync enabled but TOKEN_ENCRYPTION_KEY not set"
                    )
            # start/teardown stay no-op; the shared runner yields below.

        # One shared task group runs this mode's background tasks plus the
        # readiness health-refresh loop, then yields through the MCP session
        # manager. The group is exposed for request-path background work
        # (ADR-019 verify-on-read eviction) and cancels every task on exit.
        async with anyio.create_task_group() as tg:
            await start(tg)
            # Capture the loop's own CancelScope so shutdown stops just the loop.
            readiness_scope = await tg.start(_readiness_refresh_loop)
            _vector_sync_state.eviction_task_group = tg
            async with _mcp_session_with_login_flow(app):
                try:
                    yield
                finally:
                    logger.info("Shutting down background tasks")
                    # Request path must not spawn into a cancelling group.
                    _vector_sync_state.eviction_task_group = None
                    await teardown()
            # The readiness loop runs forever with no shutdown_event to observe,
            # and anyio waits for (not cancels) child tasks on normal exit — so
            # without this the lifespan shutdown would hang until uvicorn's
            # graceful timeout. Cancel only the loop; the task group's exit then
            # waits for the sync tasks to drain via shutdown_event (set in
            # teardown) rather than force-cancelling them mid-work.
            readiness_scope.cancel()

    # Health check endpoints for Kubernetes probes
    def health_live(request):
        """Liveness probe endpoint.

        Returns 200 OK if the application process is running.
        This is a simple check that doesn't verify external dependencies.
        """
        return JSONResponse(
            {
                "status": "alive",
                "mode": "oauth" if oauth_enabled else "basic",
            }
        )

    def health_ready(request):
        """Readiness probe endpoint.

        Gates **only** on local, cheap configuration checks (that the process is
        up and configured to serve). External dependency reachability (Nextcloud,
        Qdrant) is reported for observability but is intentionally *non-gating*
        and served from a background-refreshed cache, so the probe performs no
        external I/O and a shared-dependency blip cannot pull a single-replica
        Pod out of its Service (Deck #302).
        """
        checks: dict[str, object] = {}
        is_ready = True
        settings = get_settings()

        # --- Local, hard gates ------------------------------------------------
        if settings.nextcloud_host:
            checks["nextcloud_configured"] = "ok"
        else:
            checks["nextcloud_configured"] = "error: NEXTCLOUD_HOST not set"
            is_ready = False

        # Report the deployment mode (helps clients pick the auth flow).
        if mode == AuthMode.LOGIN_FLOW:
            checks["auth_mode"] = "oauth"
            checks["auth_configured"] = "ok"
        elif mode == AuthMode.MULTI_USER_BASIC:
            checks["auth_mode"] = "multi_user_basic"
            checks["auth_configured"] = "ok"
            # Indicate if app passwords are supported (when offline_access enabled)
            checks["supports_app_passwords"] = settings.enable_offline_access
        elif mode == AuthMode.SINGLE_USER_BASIC:
            checks["auth_mode"] = "basic"
            if settings.nextcloud_username and settings.nextcloud_password:
                checks["auth_configured"] = "ok"
            else:
                checks["auth_configured"] = "error: credentials not set"
                is_ready = False

        # --- External dependencies: reported, NON-gating ----------------------
        # Read the background-refreshed snapshot; never do I/O on the probe path.
        for name, status in _readiness_cache.snapshot().items():
            checks[name] = status.detail

        status_code = 200 if is_ready else 503
        return JSONResponse(
            {
                "status": "ready" if is_ready else "not_ready",
                "checks": checks,
            },
            status_code=status_code,
        )

    # Add Protected Resource Metadata (PRM) endpoint for OAuth mode
    routes = []

    # Add health check routes (available in both OAuth and BasicAuth modes)
    routes.append(Route("/health/live", health_live, methods=["GET"]))
    routes.append(Route("/health/ready", health_ready, methods=["GET"]))
    logger.info("Health check endpoints enabled: /health/live, /health/ready")

    # Add Nextcloud webhook receiver (queues DocumentTasks for vector sync).
    # Implementation lives in vector/webhook_receiver.py; the handler reads
    # the send-stream from request.app.state.document_send_stream.
    #
    # Security (GHSA-8vh3-g2qg-2h2c): the receiver trusts the attacker-supplied
    # user.uid in the payload and feeds it to Qdrant, so an unauthenticated
    # POST could delete/re-index any user's embeddings. The route is therefore
    # only mounted when WEBHOOK_SECRET is configured; without it the webhook
    # feature is off and vector sync still reconciles via the polling scanner.
    if settings.webhook_secret:
        routes.append(
            Route("/webhooks/nextcloud", handle_nextcloud_webhook, methods=["POST"])
        )
        logger.info("Webhook endpoint enabled: /webhooks/nextcloud")
    else:
        logger.warning(
            "Webhook endpoint disabled: WEBHOOK_SECRET is not set. "
            "/webhooks/nextcloud will return 404; vector sync relies on the "
            "polling scanner. Set WEBHOOK_SECRET to enable webhook-driven sync."
        )

    # Add management API endpoints for Nextcloud PHP app
    # Tier 1: Public endpoints (no auth required)
    # These let Astrolabe show basic server status even in single-user BasicAuth mode
    routes.append(Route("/api/v1/status", get_server_status, methods=["GET"]))
    routes.append(
        Route(
            "/api/v1/vector-sync/status",
            get_vector_sync_status,
            methods=["GET"],
        )
    )
    logger.info(
        "Public management API endpoints enabled: /api/v1/status, /api/v1/vector-sync/status"
    )

    # Tier 2+: Authenticated management endpoints (OAuth required)
    # Available in: OAuth modes OR multi-user BasicAuth with offline access
    enable_authenticated_management_apis = oauth_enabled or (
        settings.enable_multi_user_basic_auth and settings.enable_offline_access
    )
    if enable_authenticated_management_apis:
        # Admin: one-shot payload backfill (design §10.2). Requires the `admin`
        # scope (enforced inside the handler via require_admin_scope).
        routes.append(
            Route(
                "/api/v1/admin/payload-backfill",
                handle_payload_backfill,
                methods=["POST"],
            )
        )
        routes.append(
            Route(
                "/api/v1/users/{user_id}/session",
                get_user_session,
                methods=["GET"],
            )
        )
        routes.append(
            Route(
                "/api/v1/users/{user_id}/revoke",
                revoke_user_access,
                methods=["POST"],
            )
        )
        # App password endpoints for multi-user BasicAuth mode
        routes.append(
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            )
        )
        routes.append(
            Route(
                "/api/v1/users/{user_id}/app-password",
                get_app_password_status,
                methods=["GET"],
            )
        )
        routes.append(
            Route(
                "/api/v1/users/{user_id}/app-password",
                delete_app_password,
                methods=["DELETE"],
            )
        )
        routes.append(
            Route("/api/v1/vector-viz/search", vector_search, methods=["POST"])
        )
        routes.append(
            Route("/api/v1/chunk-context", get_chunk_context, methods=["GET"])
        )
        # PDF preview endpoint for Astrolabe (server-side rendering)
        routes.append(Route("/api/v1/pdf-preview", get_pdf_preview, methods=["GET"]))
        # ADR-018: Unified search endpoint for Nextcloud PHP app integration
        routes.append(Route("/api/v1/search", unified_search, methods=["POST"]))
        routes.append(Route("/api/v1/apps", get_installed_apps, methods=["GET"]))
        # Webhook management endpoints
        routes.append(Route("/api/v1/webhooks", list_webhooks, methods=["GET"]))
        routes.append(Route("/api/v1/webhooks", create_webhook, methods=["POST"]))
        routes.append(
            Route("/api/v1/webhooks/{webhook_id}", delete_webhook, methods=["DELETE"])
        )
        # Vector-sync admin: purge indexed vectors by doc type (admin consent —
        # called by Astrolabe when a source is disabled for semantic search).
        # Gated on vector_sync_enabled: without it there is no Qdrant client, so
        # the purge would 500 rather than no-op.
        if settings.vector_sync_enabled:
            routes.append(
                Route(
                    "/api/v1/vector-sync/purge",
                    purge_doc_types_route,
                    methods=["POST"],
                )
            )
            logger.info("Vector-sync admin endpoint enabled: /api/v1/vector-sync/purge")
        # Access and scope management endpoints (ADR-022)
        routes.append(
            Route(
                "/api/v1/users/{user_id}/access",
                get_user_access,
                methods=["GET"],
            )
        )
        routes.append(
            Route(
                "/api/v1/users/{user_id}/scopes",
                update_user_scopes,
                methods=["PATCH"],
            )
        )
        routes.append(Route("/api/v1/scopes", list_supported_scopes, methods=["GET"]))
        logger.info(
            "Authenticated management API endpoints enabled: "
            "/api/v1/users/{user_id}/session, /api/v1/users/{user_id}/revoke, "
            "/api/v1/users/{user_id}/app-password, /api/v1/users/{user_id}/access, "
            "/api/v1/users/{user_id}/scopes, /api/v1/scopes, "
            "/api/v1/vector-viz/search, /api/v1/search, /api/v1/apps, "
            "/api/v1/webhooks, /api/v1/pdf-preview"
        )

    # Note: Metrics endpoint is NOT exposed on main HTTP port for security reasons.
    # Metrics are served on dedicated port via setup_metrics() (default: 9090)

    # Determine if OAuth provisioning is available
    # This is true for:
    # 1. OAuth modes (primary auth method for MCP operations)
    # 2. Multi-user BasicAuth with offline access (hybrid mode)
    oauth_provisioning_available = oauth_enabled or (
        mode == AuthMode.MULTI_USER_BASIC
        and settings.enable_offline_access
        and multi_user_token_verifier is not None  # Ensure OAuth setup succeeded
    )

    if oauth_provisioning_available:
        logger.info(
            "OAuth provisioning routes enabled for mode: %s (oauth_enabled=%s, hybrid_mode=%s)",
            mode.value,
            oauth_enabled,
            not oauth_enabled,
        )

        def oauth_protected_resource_metadata(request):
            """RFC 9728 Protected Resource Metadata endpoint.

            Dynamically discovers supported scopes from registered MCP tools.
            This ensures the advertised scopes always match the actual tool requirements.

            The 'resource' field is set to the MCP server's public URL (RFC 9728 requires a URL).
            This is used as the audience in access tokens via the resource parameter (RFC 8707).
            The introspection controller matches this URL to the MCP server's client via resource_url field.

            ADR-023: authorization_servers points to the MCP server itself (AS proxy)
            so that clients authenticate through the proxy and tokens have correct audience.
            """
            # RFC 9728 requires resource to be a URL (not a client ID)
            # Use the MCP server's public URL
            mcp_server_url = settings.nextcloud_mcp_server_url
            if not mcp_server_url:
                # Fallback derived from the configured port (see helper).
                mcp_server_url = _default_mcp_server_url()

            # Dynamically discover all scopes from registered tools
            # This provides a single source of truth based on @require_scopes decorators
            supported_scopes = discover_all_scopes(mcp)

            # ADR-023: Point authorization_servers to the MCP server itself.
            # The MCP server acts as an OAuth AS proxy, forwarding to Nextcloud
            # with its own client_id so tokens have the correct audience.
            return JSONResponse(
                {
                    "resource": f"{mcp_server_url}/mcp",  # RFC 9728: must be a URL
                    "scopes_supported": supported_scopes,
                    "authorization_servers": [mcp_server_url],
                    "bearer_methods_supported": ["header"],
                    "resource_signing_alg_values_supported": ["RS256"],
                }
            )

        # Register PRM endpoint at both path-based and root locations per RFC 9728
        # Path-based discovery: /.well-known/oauth-protected-resource{path}
        routes.append(
            Route(
                "/.well-known/oauth-protected-resource/mcp",
                oauth_protected_resource_metadata,
                methods=["GET"],
            )
        )
        # Root discovery (fallback): /.well-known/oauth-protected-resource
        routes.append(
            Route(
                "/.well-known/oauth-protected-resource",
                oauth_protected_resource_metadata,
                methods=["GET"],
            )
        )
        logger.info(
            "Protected Resource Metadata (PRM) endpoints enabled (path-based + root)"
        )

        # Add unified OAuth callback endpoint supporting both flows
        routes.append(Route("/oauth/callback", oauth_callback, methods=["GET"]))
        logger.info(
            "OAuth unified callback enabled: /oauth/callback?flow={browser|provisioning}"
        )

        # Add OAuth resource provisioning routes (ADR-004 Progressive Consent Flow 2)
        routes.append(
            Route(
                "/oauth/authorize-nextcloud",
                oauth_authorize_nextcloud,
                methods=["GET"],
            )
        )
        # Keep old callback endpoint as backwards-compatible alias
        routes.append(
            Route(
                "/oauth/callback-nextcloud",
                oauth_callback_nextcloud,
                methods=["GET"],
            )
        )
        logger.info(
            "OAuth resource provisioning routes enabled: /oauth/authorize-nextcloud, /oauth/callback-nextcloud (Flow 2)"
        )

    # Add OAuth Flow 1 routes (MCP client login) - ONLY for OAuth modes
    # Multi-user BasicAuth uses hybrid mode with only Flow 2 (resource provisioning)
    if oauth_enabled:
        routes.append(Route("/oauth/authorize", oauth_authorize, methods=["GET"]))

        # ADR-023: AS proxy endpoints — MCP server acts as its own OAuth AS
        routes.append(Route("/oauth/token", oauth_token_endpoint, methods=["POST"]))
        routes.append(Route("/oauth/register", oauth_register_proxy, methods=["POST"]))
        routes.append(
            Route(
                "/.well-known/oauth-authorization-server",
                oauth_as_metadata,
                methods=["GET"],
            )
        )
        logger.info(
            "OAuth AS proxy routes enabled: /oauth/authorize, /oauth/token, "
            "/oauth/register, /.well-known/oauth-authorization-server (ADR-023)"
        )

    # Add browser OAuth login routes for Management API access
    # Available in OAuth modes AND multi-user BasicAuth with offline access
    # (hybrid mode). Separate from MCP tool auth - Management API uses OAuth
    if oauth_provisioning_available:
        routes.append(
            Route("/oauth/login", oauth_login, methods=["GET"], name="oauth_login")
        )
        # Keep old callback endpoint as backwards-compatible alias
        routes.append(
            Route(
                "/oauth/login-callback",
                oauth_login_callback,
                methods=["GET"],
                name="oauth_login_callback",
            )
        )
        # POST-only: defends against passive CSRF (e.g. <img src="…/logout">)
        # — see PR #758 finding 5.
        routes.append(
            Route("/oauth/logout", oauth_logout, methods=["POST"], name="oauth_logout")
        )
        logger.info(
            "Browser OAuth routes enabled: /oauth/login, /oauth/login-callback (legacy), /oauth/logout"
        )

    # Add user info routes (available in both BasicAuth and OAuth modes)
    # Create a separate Starlette app for browser routes that need session auth
    # This prevents SessionAuthBackend from interfering with FastMCP's OAuth
    browser_routes = [
        Route("/", user_info_html, methods=["GET"]),  # /app → user info with all tabs
        Route(
            "/revoke",
            revoke_session,
            methods=["POST"],
            name="revoke_session_endpoint",
        ),  # /app/revoke → revoke_session
        # Vector sync status fragment (htmx polling)
        Route(
            "/vector-sync/status",
            vector_sync_status_fragment,
            methods=["GET"],
        ),  # /app/vector-sync/status
        # Vector visualization routes
        Route(
            "/vector-viz", vector_visualization_html, methods=["GET"]
        ),  # /app/vector-viz
        Route(
            "/vector-viz/search",
            vector_visualization_search,
            methods=["GET"],
        ),  # /app/vector-viz/search
        Route(
            "/chunk-context",
            chunk_context_endpoint,
            methods=["GET"],
        ),  # /app/chunk-context
        # Webhook management routes (admin-only)
        Route("/webhooks", webhook_management_pane, methods=["GET"]),  # /app/webhooks
        Route(
            "/webhooks/enable/{preset_id:str}",
            enable_webhook_preset,
            methods=["POST"],
        ),
        Route(
            "/webhooks/disable/{preset_id:str}",
            disable_webhook_preset,
            methods=["DELETE"],
        ),
    ]

    # Login Flow v2 web provisioning (only when Login Flow is enabled)
    if settings.enable_login_flow:
        browser_routes += [
            Route("/provision", provision_page, methods=["GET"]),  # /app/provision
            Route(
                "/provision/status", provision_status, methods=["GET"]
            ),  # /app/provision/status
        ]

    # Add static files mount if directory exists
    static_dir = os.path.join(os.path.dirname(__file__), "auth", "static")
    if os.path.isdir(static_dir):
        browser_routes.append(
            Mount("/static", StaticFiles(directory=static_dir), name="static")
        )
        logger.info("Mounted static files from %s", static_dir)

    browser_app = Starlette(routes=browser_routes)
    browser_app.add_middleware(
        AuthenticationMiddleware,  # type: ignore[invalid-argument-type]
        backend=SessionAuthBackend(oauth_enabled=oauth_enabled),
    )

    # Add redirect from /app to /app/ (Starlette requires trailing slash for mounted apps)
    routes.append(
        Route("/app", lambda request: RedirectResponse("/app/", status_code=307))
    )

    # Mount browser app at /app (webapp and admin routes)
    routes.append(Mount("/app", app=browser_app))
    logger.info("App routes with session auth: /app, /app/webhooks, /app/revoke")

    # Favicon for connector directory discovery (Google favicon service)
    favicon_path = os.path.join(
        os.path.dirname(__file__), "auth", "static", "favicon.png"
    )
    if os.path.isfile(favicon_path):
        routes.append(
            Route(
                "/favicon.ico",
                lambda request: FileResponse(favicon_path, media_type="image/png"),
            )
        )

    # Mount FastMCP at root last (catch-all, handles OAuth via token_verifier)
    routes.append(Mount("/", app=mcp_app))

    app = Starlette(routes=routes, lifespan=starlette_lifespan)
    logger.info(
        "Routes: /user/* with SessionAuth, /mcp with FastMCP OAuth Bearer tokens"
    )

    # Store supported scopes on app.state for AS metadata endpoint (ADR-023)
    if oauth_enabled:
        app.state.supported_scopes = discover_all_scopes(mcp)

    # Add debugging middleware to log Authorization headers and client capabilities
    @app.middleware("http")
    async def log_auth_headers(request, call_next):
        auth_header = request.headers.get("authorization")
        if request.url.path.startswith("/mcp"):
            if auth_header:
                # Log first 50 chars of token for debugging
                token_preview = (
                    auth_header[:50] + "..." if len(auth_header) > 50 else auth_header
                )
                logger.info("🔑 /mcp request with Authorization: %s", token_preview)
            else:
                # Only warn about missing Authorization in OAuth mode
                # In BasicAuth mode, /mcp requests without Authorization are expected
                if oauth_enabled:
                    logger.warning(
                        "⚠️  /mcp request WITHOUT Authorization header from %s",
                        request.client,
                    )

            # Log client capabilities on initialize request
            if request.method == "POST":
                # Read body to check for initialize request
                # Starlette caches the body internally, so it's safe to read here
                body = await request.body()
                try:
                    data = json.loads(body)
                    # Check if this is an initialize request
                    if data.get("method") == "initialize":
                        params = data.get("params", {})
                        capabilities = params.get("capabilities", {})
                        client_info = params.get("clientInfo", {})

                        logger.info(
                            "🔌 MCP client connected: %s v%s",
                            client_info.get("name", "unknown"),
                            client_info.get("version", "unknown"),
                        )

                        # Log capabilities in a structured way
                        cap_summary = []
                        # Check for presence using 'in' not truthiness (empty dict {} counts as having capability)
                        if "roots" in capabilities:
                            cap_summary.append("roots")
                        if "sampling" in capabilities:
                            cap_summary.append("sampling")
                        if "experimental" in capabilities:
                            cap_summary.append(
                                f"experimental({len(capabilities['experimental'])} features)"
                            )

                        logger.info(
                            "📋 Client capabilities: %s",
                            ", ".join(cap_summary) if cap_summary else "none",
                        )
                        # Log full capabilities at INFO level to diagnose capability issues
                        logger.info(
                            "Full capabilities JSON: %s", json.dumps(capabilities)
                        )
                except Exception as e:
                    # Don't fail the request if logging fails
                    logger.debug(
                        "Failed to parse MCP request for capability logging: %s", e
                    )

        response = await call_next(request)
        return response

    # Log the inbound User-Agent on management API and webhook receiver routes
    # so we can tell which Astrolabe (or other PHP-side client) build is
    # talking to the backend. Astrolabe sends ``Nextcloud-Astrolabe/<version>``.
    _UA_LOGGED_PATH_PREFIXES = ("/api/v1/", "/webhooks/nextcloud")

    @app.middleware("http")
    async def log_client_user_agent(request, call_next):
        path = request.url.path
        if path.startswith(_UA_LOGGED_PATH_PREFIXES):
            ua = request.headers.get("user-agent") or "(none)"
            logger.info(
                "%s %s from %s",
                request.method,
                path,
                ua,
                extra={
                    "user_agent": ua,
                    "http_method": request.method,
                    "http_path": path,
                },
            )
        return await call_next(request)

    # Add CORS middleware to allow browser-based clients like MCP Inspector
    app.add_middleware(
        CORSMiddleware,  # type: ignore[invalid-argument-type]
        allow_origins=["*"],  # Allow all origins for development
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Add observability middleware (metrics + tracing)
    if settings.metrics_enabled or settings.otel_exporter_otlp_endpoint:
        app.add_middleware(ObservabilityMiddleware)  # type: ignore[invalid-argument-type]
        logger.info("Observability middleware enabled (metrics and/or tracing)")

    # Add exception handler for scope challenges (OAuth mode only)
    if oauth_enabled:

        @app.exception_handler(InsufficientScopeError)
        async def handle_insufficient_scope(request, exc: InsufficientScopeError):
            """Return 403 with WWW-Authenticate header for scope challenges."""
            resource_url = (
                settings.nextcloud_mcp_server_url or _default_mcp_server_url()
            )
            scope_str = " ".join(exc.missing_scopes)

            return JSONResponse(
                status_code=403,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer error="insufficient_scope", '
                        f'scope="{scope_str}", '
                        f'resource_metadata="{resource_url}/.well-known/oauth-protected-resource/mcp"'
                    )
                },
                content={
                    "error": "insufficient_scope",
                    "scopes_required": exc.missing_scopes,
                },
            )

        logger.info("WWW-Authenticate scope challenge handler enabled")

    # Apply BasicAuthMiddleware for multi-user BasicAuth pass-through mode
    if settings.enable_multi_user_basic_auth:
        app = BasicAuthMiddleware(app)
        logger.info(
            "BasicAuthMiddleware enabled - multi-user BasicAuth pass-through mode active"
        )

    return app
