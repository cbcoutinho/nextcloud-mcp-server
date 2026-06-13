import logging
import os
from importlib.metadata import version

import click
import uvicorn

from nextcloud_mcp_server.config import (
    Settings,
    get_database_url,
    get_settings,
    is_ephemeral_token_db,
)
from nextcloud_mcp_server.migrations import (
    create_migration,
    downgrade_database,
    get_current_revision,
    show_migration_history,
    upgrade_database,
)
from nextcloud_mcp_server.observability import (
    get_uvicorn_logging_config,
    setup_logging,
    setup_metrics,
    setup_tracing,
)
from nextcloud_mcp_server.server import AVAILABLE_APPS

from .app import get_app

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--host", "-h", default="127.0.0.1", show_default=True, help="Server host"
)
@click.option(
    "--port", "-p", type=int, default=8000, show_default=True, help="Server port"
)
@click.option(
    "--log-level",
    "-l",
    default="info",
    show_default=True,
    type=click.Choice(["critical", "error", "warning", "info", "debug", "trace"]),
    help="Logging level",
)
@click.option(
    "--transport",
    "-t",
    default="streamable-http",
    show_default=True,
    type=click.Choice(["streamable-http", "http", "stdio"]),
    help="MCP transport protocol",
)
@click.option(
    "--enable-app",
    "-e",
    multiple=True,
    type=click.Choice(sorted(AVAILABLE_APPS.keys())),
    help="Enable specific Nextcloud app APIs. Can be specified multiple times. If not specified, all apps are enabled.",
)
@click.option(
    "--oauth/--no-oauth",
    default=None,
    help="Force OAuth mode (if enabled) or BasicAuth mode (if disabled). By default, auto-detected based on environment variables.",
)
@click.option(
    "--oauth-client-id",
    envvar="NEXTCLOUD_OIDC_CLIENT_ID",
    help="OAuth client ID (can also use NEXTCLOUD_OIDC_CLIENT_ID env var)",
)
@click.option(
    "--oauth-client-secret",
    envvar="NEXTCLOUD_OIDC_CLIENT_SECRET",
    help="OAuth client secret (can also use NEXTCLOUD_OIDC_CLIENT_SECRET env var)",
)
@click.option(
    "--mcp-server-url",
    envvar="NEXTCLOUD_MCP_SERVER_URL",
    default="http://localhost:8000",
    show_default=True,
    help="MCP server URL for OAuth callbacks (can also use NEXTCLOUD_MCP_SERVER_URL env var)",
)
@click.option(
    "--nextcloud-host",
    envvar="NEXTCLOUD_HOST",
    help="Nextcloud instance URL (can also use NEXTCLOUD_HOST env var)",
)
@click.option(
    "--nextcloud-username",
    envvar="NEXTCLOUD_USERNAME",
    help="Nextcloud username for BasicAuth (can also use NEXTCLOUD_USERNAME env var)",
)
@click.option(
    "--nextcloud-password",
    envvar="NEXTCLOUD_PASSWORD",
    help="Nextcloud password for BasicAuth (can also use NEXTCLOUD_PASSWORD env var)",
)
@click.option(
    "--oauth-scopes",
    envvar="NEXTCLOUD_OIDC_SCOPES",
    default="openid profile email notes.read notes.write calendar.read calendar.write todo.read todo.write contacts.read contacts.write cookbook.read cookbook.write deck.read deck.write tables.read tables.write files.read files.write sharing.read sharing.write",
    show_default=True,
    help="OAuth scopes to request during client registration. These define the maximum allowed scopes for the client. Note: Actual supported scopes are discovered dynamically from MCP tools at runtime. (can also use NEXTCLOUD_OIDC_SCOPES env var)",
)
@click.option(
    "--oauth-token-type",
    envvar="NEXTCLOUD_OIDC_TOKEN_TYPE",
    default="bearer",
    show_default=True,
    type=click.Choice(["bearer", "jwt"], case_sensitive=False),
    help="OAuth token type (can also use NEXTCLOUD_OIDC_TOKEN_TYPE env var)",
)
@click.option(
    "--public-issuer-url",
    envvar="NEXTCLOUD_PUBLIC_ISSUER_URL",
    help="Public issuer URL for OAuth (can also use NEXTCLOUD_PUBLIC_ISSUER_URL env var)",
)
def run(
    host: str,
    port: int,
    log_level: str,
    transport: str,
    enable_app: tuple[str, ...],
    oauth: bool | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    mcp_server_url: str,
    nextcloud_host: str | None,
    nextcloud_username: str | None,
    nextcloud_password: str | None,
    oauth_scopes: str,
    oauth_token_type: str,
    public_issuer_url: str | None,
):
    """
    Run the Nextcloud MCP server.

    \b
    Authentication Modes:
      - BasicAuth: Set NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD
      - OAuth: Leave USERNAME/PASSWORD unset (requires OIDC app enabled)

    \b
    Examples:
      # BasicAuth mode with CLI options
      $ nextcloud-mcp-server --nextcloud-host=https://cloud.example.com \\
          --nextcloud-username=admin --nextcloud-password=secret

      # BasicAuth mode with env vars (recommended for credentials)
      $ export NEXTCLOUD_HOST=https://cloud.example.com
      $ export NEXTCLOUD_USERNAME=admin
      $ export NEXTCLOUD_PASSWORD=secret
      $ nextcloud-mcp-server --host 0.0.0.0 --port 8000

      # OAuth mode with auto-registration
      $ nextcloud-mcp-server --nextcloud-host=https://cloud.example.com --oauth

      # OAuth mode with pre-configured client
      $ nextcloud-mcp-server --nextcloud-host=https://cloud.example.com --oauth \\
          --oauth-client-id=xxx --oauth-client-secret=yyy

      # OAuth mode with custom scopes and JWT tokens
      $ nextcloud-mcp-server --nextcloud-host=https://cloud.example.com --oauth \\
          --oauth-scopes="openid notes.read notes.write" --oauth-token-type=jwt

      # OAuth with public issuer URL (for Docker/proxy setups)
      $ nextcloud-mcp-server --nextcloud-host=http://app --oauth \\
          --public-issuer-url=http://localhost:8080

      # stdio transport for local use (e.g. Claude Code)
      $ nextcloud-mcp-server run --transport stdio
    """
    # Set env vars from CLI options if provided
    if nextcloud_host:
        os.environ["NEXTCLOUD_HOST"] = nextcloud_host
    if nextcloud_username:
        os.environ["NEXTCLOUD_USERNAME"] = nextcloud_username
    if nextcloud_password:
        os.environ["NEXTCLOUD_PASSWORD"] = nextcloud_password
    if oauth_client_id:
        os.environ["NEXTCLOUD_OIDC_CLIENT_ID"] = oauth_client_id
    if oauth_client_secret:
        os.environ["NEXTCLOUD_OIDC_CLIENT_SECRET"] = oauth_client_secret
    if oauth_scopes:
        os.environ["NEXTCLOUD_OIDC_SCOPES"] = oauth_scopes
    if oauth_token_type:
        os.environ["NEXTCLOUD_OIDC_TOKEN_TYPE"] = oauth_token_type
    if mcp_server_url:
        os.environ["NEXTCLOUD_MCP_SERVER_URL"] = mcp_server_url
    if public_issuer_url:
        os.environ["NEXTCLOUD_PUBLIC_ISSUER_URL"] = public_issuer_url

    # Force OAuth mode if explicitly requested
    if oauth is True:
        # Clear username/password to force OAuth mode
        if "NEXTCLOUD_USERNAME" in os.environ:
            click.echo(
                "Warning: --oauth flag set, ignoring NEXTCLOUD_USERNAME", err=True
            )
            del os.environ["NEXTCLOUD_USERNAME"]
        if "NEXTCLOUD_PASSWORD" in os.environ:
            click.echo(
                "Warning: --oauth flag set, ignoring NEXTCLOUD_PASSWORD", err=True
            )
            del os.environ["NEXTCLOUD_PASSWORD"]

        # Validate OAuth configuration
        nextcloud_host = os.getenv("NEXTCLOUD_HOST")
        if not nextcloud_host:
            raise click.ClickException(
                "OAuth mode requires NEXTCLOUD_HOST environment variable to be set"
            )

        # Check if we have client credentials OR if dynamic registration is possible
        has_client_creds = os.getenv("NEXTCLOUD_OIDC_CLIENT_ID") and os.getenv(
            "NEXTCLOUD_OIDC_CLIENT_SECRET"
        )

        if not has_client_creds:
            # No client credentials - will attempt dynamic registration
            # Show helpful message before server starts
            click.echo("", err=True)
            click.echo("OAuth Configuration:", err=True)
            click.echo("  Mode: Dynamic Client Registration", err=True)
            click.echo("  Host: " + nextcloud_host, err=True)
            click.echo("  Storage: SQLite (TOKEN_STORAGE_DB)", err=True)
            click.echo("", err=True)
            click.echo(
                "Note: Make sure 'Dynamic Client Registration' is enabled", err=True
            )
            click.echo("      in your Nextcloud OIDC app settings.", err=True)
            click.echo("", err=True)
        else:
            click.echo("", err=True)
            click.echo("OAuth Configuration:", err=True)
            click.echo("  Mode: Pre-configured Client", err=True)
            click.echo("  Host: " + nextcloud_host, err=True)
            click.echo(
                "  Client ID: "
                + os.getenv("NEXTCLOUD_OIDC_CLIENT_ID", "")[:16]
                + "...",
                err=True,
            )
            click.echo("", err=True)

    elif oauth is False:
        # Force BasicAuth mode - verify credentials exist
        if not os.getenv("NEXTCLOUD_USERNAME") or not os.getenv("NEXTCLOUD_PASSWORD"):
            raise click.ClickException(
                "--no-oauth flag set but NEXTCLOUD_USERNAME or NEXTCLOUD_PASSWORD not set"
            )

    enabled_apps = list(enable_app) if enable_app else None

    if transport == "stdio":
        if oauth is True:
            raise click.ClickException(
                "stdio transport does not support OAuth mode. "
                "Use single-user BasicAuth with NEXTCLOUD_HOST, "
                "NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD."
            )
        from .stdio import get_stdio_mcp  # noqa: PLC0415

        try:
            mcp = get_stdio_mcp(enabled_apps=enabled_apps)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        mcp.run(transport="stdio")
        return

    app = get_app(transport=transport, enabled_apps=enabled_apps)

    # Get observability settings and create uvicorn logging config
    settings = get_settings()
    uvicorn_log_config = get_uvicorn_logging_config(
        log_format=settings.log_format,
        log_level=settings.log_level,
        include_trace_context=settings.log_include_trace_context,
    )

    uvicorn.run(
        app=app,
        host=host,
        port=port,
        log_level=log_level,
        log_config=uvicorn_log_config,
    )


def _init_worker_observability(settings: Settings) -> None:
    """Configure logging, metrics, and tracing for the standalone ingest worker."""
    # Mirrors app.py's lifespan bootstrap; without it the worker's astrolabe_*
    # metrics and document_processor.parse spans are invisible in external mode.
    # Structured logging first, so every subsequent startup line is JSON like
    # the API's — the worker entrypoint never went through uvicorn's log_config.
    setup_logging(
        log_format=settings.log_format,
        log_level=settings.log_level,
        include_trace_context=settings.log_include_trace_context,
    )

    if settings.metrics_enabled:
        setup_metrics(port=settings.metrics_port)
        logger.info(
            "Prometheus metrics enabled on dedicated port %s", settings.metrics_port
        )

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


@click.command()
@click.option(
    "--concurrency",
    "-c",
    type=int,
    default=None,
    help="Max concurrent jobs. Defaults to VECTOR_SYNC_PROCESSOR_WORKERS.",
)
@click.option(
    "--tier",
    type=click.Choice(["fast", "structured", "ocr"]),
    default=None,
    help=(
        "Run only this extraction tier's queue (Deck #323). Omit to drain ALL "
        "tier queues in one process (single-Deployment / dev); set it to run one "
        "tier per Deployment so the fleets scale independently."
    ),
)
def worker(concurrency: int | None, tier: str | None):
    """Run the ingest worker (Deck #183, per-tier fleets #323).

    \b
    Drains the per-tenant Postgres ingest queue (procrastinate): for each
    deferred document it fetches the content as the owning user, parses, chunks,
    embeds, and upserts into Qdrant. This is the scale-to-zero ``worker`` role of
    the api/worker split; run it as a separate Deployment from the API pod.

    \b
    With --tier the worker drains only that tier's queue (``ingest-<tier>``), so
    a CPU-bound ``fast`` fleet, an in-cluster ``structured`` fleet, and a paid
    ``ocr`` fleet scale independently. Without it, all tier queues are drained in
    one process (handy for dev / a single Deployment). A low-quality parse hops
    the job to the next tier's queue automatically (see TieredEscalationStrategy).

    \b
    Requires INGEST_QUEUE=postgres (a PostgreSQL DATABASE_URL); procrastinate is
    Postgres-only.

    \b
    Example:
      $ export DATABASE_URL=postgresql+asyncpg://mcp:mcp@db/mcp
      $ nextcloud-mcp-server worker -c 4 --tier fast
    """
    import anyio  # noqa: PLC0415

    settings = get_settings()
    if settings.ingest_queue != "postgres":
        raise click.ClickException(
            "worker requires INGEST_QUEUE=postgres (a PostgreSQL DATABASE_URL); "
            f"resolved INGEST_QUEUE={settings.ingest_queue!r}"
        )

    # Initialize observability here, not in a lifespan — the worker never runs
    # uvicorn, so it skips app.py's bootstrap (the WHY lives in the helper's
    # docstring). Done after the queue check so a misconfig fails fast.
    _init_worker_observability(settings)

    from nextcloud_mcp_server.vector.queue.procrastinate import (  # noqa: PLC0415
        ALL_INGEST_QUEUES,
        INGEST_QUEUE_MAINTENANCE,
        LEGACY_INGEST_QUEUE,
        TIER_QUEUES,
        apply_ingest_queue_schema,
        get_procrastinate_app,
    )

    # Which queues this process drains. A single tier -> just its queue; no tier
    # -> every tier queue PLUS the legacy single queue, so a rolling upgrade
    # never strands jobs deferred under the pre-#323 name. Every worker also
    # drains the maintenance queue so the periodic stalled-job reclaim fires
    # regardless of which tier(s) are scaled up (procrastinate dedups the
    # periodic, so multiple drainers don't multiply the reclaim).
    if tier is not None:
        queues = [TIER_QUEUES[tier], INGEST_QUEUE_MAINTENANCE]
    else:
        queues = [*ALL_INGEST_QUEUES, LEGACY_INGEST_QUEUE, INGEST_QUEUE_MAINTENANCE]

    # This is the consumer side of the distributed (postgres) ingest backend.
    # Unlike the in-process anyio pool, the worker talks to procrastinate's App
    # directly (run_worker_async), so it does NOT go through IngestTransport —
    # DistributedTransport.run_consumers is a deliberate no-op precisely because
    # this separate process is the consumer (see vector/queue/transport.py).
    workers = concurrency or settings.vector_sync_processor_workers
    app = get_procrastinate_app()

    # Register the configured document processors (Unstructured / Tesseract /
    # custom HTTP) in the worker process. The always-on API pod does this in its
    # lifespan; the worker has its own startup path, so without this the worker
    # would silently fall back to the import-time-registered PyMuPDF only.
    from nextcloud_mcp_server.app import initialize_document_processors  # noqa: PLC0415

    initialize_document_processors()

    async def _run() -> None:
        # Open the connector pool once and reuse it for both the defensive
        # schema apply (the always-on API pod is the authoritative applier) and
        # the worker loop — manage_connection=False avoids a redundant
        # open/close cycle on startup.
        async with app.open_async():
            await apply_ingest_queue_schema(app, manage_connection=False)
            # Structured log (not click.echo) so it lands in the JSON / OTel
            # pipeline like every other startup message.
            logger.info(
                "Ingest worker started: tier=%s queues=%s concurrency=%s "
                "delete_succeeded=%s",
                tier or "all",
                queues,
                workers,
                settings.ingest_delete_succeeded_jobs,
            )
            await app.run_worker_async(
                queues=queues,
                concurrency=workers,
                install_signal_handlers=True,
                # Drop succeeded jobs (default) so the queue table stays lean and
                # the KEDA queue-depth metric reflects only outstanding work; set
                # INGEST_DELETE_SUCCEEDED_JOBS=false to retain them for audit.
                delete_jobs="successful"
                if settings.ingest_delete_succeeded_jobs
                else "never",
            )

    anyio.run(_run)


@click.group()
def db():
    """Database migration management commands."""
    pass


def _resolve_db_url(database_url: str | None, database_path: str | None) -> str:
    """Pick the database URL for a CLI subcommand.

    Priority: explicit ``--database-url`` > legacy ``--database-path``
    (treated as a SQLite file) > :func:`get_database_url` (honors
    ``DATABASE_URL`` env or falls back to the ephemeral SQLite tempfile).
    """
    if database_url:
        return database_url
    if database_path:
        return f"sqlite+aiosqlite:///{database_path}"
    return get_database_url()


def _warn_if_ephemeral(database_url: str) -> None:
    """Warn when the resolved URL is the per-process SQLite tempfile."""
    if not database_url.startswith(
        "sqlite+aiosqlite:///"
    ) and not database_url.startswith("sqlite:///"):
        return
    path = database_url.split("///", 1)[1]
    if is_ephemeral_token_db(path):
        click.echo(
            click.style(
                f"⚠ Using ephemeral tempfile {path}; changes "
                "will be lost on exit. Pass --database-url / --database-path "
                "or set DATABASE_URL / TOKEN_STORAGE_DB to operate on a "
                "persistent database.",
                fg="yellow",
            ),
            err=True,
        )


def _db_target_options(fn):
    """Attach the shared ``--database-url`` / ``--database-path`` options.

    Using a decorator factory rather than ``**kwargs`` dict-expansion so
    static type checkers (ty) see ``click.option`` called with literal
    keyword arguments, which is the only form it's typed to accept.
    """
    fn = click.option(
        "--database-path",
        "-d",
        envvar="TOKEN_STORAGE_DB",
        default=None,
        help="SQLite database file path. Equivalent to "
        "--database-url sqlite+aiosqlite:///<path>.",
    )(fn)
    fn = click.option(
        "--database-url",
        "-u",
        envvar="DATABASE_URL",
        default=None,
        help="SQLAlchemy URL (e.g. postgresql+asyncpg://...). Wins over --database-path.",
    )(fn)
    return fn


@db.command()
@_db_target_options
@click.option(
    "--revision",
    "-r",
    default="head",
    show_default=True,
    help="Target revision (default: head for latest)",
)
def upgrade(database_url: str | None, database_path: str | None, revision: str):
    """Upgrade database to a specific revision.

    \b
    Examples:
      # Upgrade to latest version
      $ nextcloud-mcp-server db upgrade

      # Upgrade a Postgres backend
      $ nextcloud-mcp-server db upgrade -u postgresql+asyncpg://mcp:mcp@db/mcp

      # Use custom SQLite path
      $ nextcloud-mcp-server db upgrade -d /path/to/tokens.db
    """
    url = _resolve_db_url(database_url, database_path)
    _warn_if_ephemeral(url)
    try:
        click.echo(f"Upgrading database to revision: {revision}")
        upgrade_database(url, revision)
        # Apply procrastinate's ingest-queue schema on Postgres so a one-shot
        # migration/init job provisions everything the api + worker roles need
        # (Deck #183). Idempotent + lazy import (Postgres-only extra).
        from nextcloud_mcp_server.config import is_sqlite_url  # noqa: PLC0415

        if not is_sqlite_url(url):
            import anyio  # noqa: PLC0415

            from nextcloud_mcp_server.vector.queue.procrastinate import (  # noqa: PLC0415
                apply_ingest_queue_schema,
                build_app_for_url,
            )

            anyio.run(apply_ingest_queue_schema, build_app_for_url(url))
            click.echo(click.style("✓ Ingest queue schema applied", fg="green"))
        click.echo(click.style("✓ Database upgraded successfully", fg="green"))
    except Exception as e:
        click.echo(click.style(f"✗ Upgrade failed: {e}", fg="red"), err=True)
        raise click.ClickException(str(e))


@db.command()
@_db_target_options
@click.option(
    "--revision",
    "-r",
    default="-1",
    show_default=True,
    help="Target revision (default: -1 for previous version)",
)
@click.confirmation_option(
    prompt="Are you sure you want to downgrade the database? This may result in data loss."
)
def downgrade(database_url: str | None, database_path: str | None, revision: str):
    """Downgrade database to a specific revision.

    WARNING: This may result in data loss! Use with caution.
    """
    url = _resolve_db_url(database_url, database_path)
    _warn_if_ephemeral(url)
    try:
        click.echo(f"Downgrading database to revision: {revision}")
        downgrade_database(url, revision)
        click.echo(click.style("✓ Database downgraded successfully", fg="green"))
    except Exception as e:
        click.echo(click.style(f"✗ Downgrade failed: {e}", fg="red"), err=True)
        raise click.ClickException(str(e))


@db.command()
@_db_target_options
def current(database_url: str | None, database_path: str | None):
    """Show current database revision."""
    url = _resolve_db_url(database_url, database_path)
    _warn_if_ephemeral(url)
    try:
        revision = get_current_revision(url)
        if revision:
            click.echo(f"Current revision: {click.style(revision, fg='cyan')}")
        else:
            click.echo(
                click.style(
                    "Database is not versioned (no alembic_version table)", fg="yellow"
                )
            )
    except Exception as e:
        click.echo(
            click.style(f"✗ Failed to get current revision: {e}", fg="red"), err=True
        )
        raise click.ClickException(str(e))


@db.command()
@_db_target_options
def history(database_url: str | None, database_path: str | None):
    """Show migration history."""
    url = _resolve_db_url(database_url, database_path)
    _warn_if_ephemeral(url)
    try:
        click.echo("Migration history:")
        show_migration_history(url)
    except Exception as e:
        click.echo(click.style(f"✗ Failed to show history: {e}", fg="red"), err=True)
        raise click.ClickException(str(e))


@db.command()
@click.argument("message")
def migrate(message: str):
    """Create a new migration script (developers only).

    The MESSAGE argument describes the changes in this migration.

    \b
    Examples:
      $ nextcloud-mcp-server db migrate "add user preferences table"
      $ nextcloud-mcp-server db migrate "add index on refresh_tokens.user_id"

    Note: You must manually edit the generated migration file to add SQL statements.
    """
    try:
        click.echo(f"Creating new migration: {message}")
        create_migration(message)
        click.echo(click.style("✓ Migration created successfully", fg="green"))
        click.echo(
            "Edit the migration file in alembic/versions/ to add upgrade/downgrade SQL."
        )
    except Exception as e:
        click.echo(
            click.style(f"✗ Failed to create migration: {e}", fg="red"), err=True
        )
        raise click.ClickException(str(e))


# Create CLI group with subcommands
@click.group()
@click.version_option(
    version=version("nextcloud-mcp-server"), prog_name="nextcloud-mcp-server"
)
def cli():
    pass


cli.add_command(run)
cli.add_command(worker)
cli.add_command(db)


if __name__ == "__main__":
    cli()
