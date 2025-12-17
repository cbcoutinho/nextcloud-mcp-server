import os

import click
import uvicorn

from nextcloud_mcp_server.config import (
    get_settings,
)
from nextcloud_mcp_server.observability import get_uvicorn_logging_config

from .app import get_app


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
    type=click.Choice(["streamable-http", "http"]),
    help="MCP transport protocol",
)
@click.option(
    "--enable-app",
    "-e",
    multiple=True,
    type=click.Choice(
        ["notes", "tables", "webdav", "calendar", "contacts", "cookbook", "deck"]
    ),
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
    default="openid profile email notes:read notes:write calendar:read calendar:write todo:read todo:write contacts:read contacts:write cookbook:read cookbook:write deck:read deck:write tables:read tables:write files:read files:write sharing:read sharing:write",
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
          --oauth-scopes="openid notes:read notes:write" --oauth-token-type=jwt

      # OAuth with public issuer URL (for Docker/proxy setups)
      $ nextcloud-mcp-server --nextcloud-host=http://app --oauth \\
          --public-issuer-url=http://localhost:8080
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


@click.group()
def db():
    """Database migration management commands."""
    pass


@db.command()
@click.option(
    "--database-path",
    "-d",
    envvar="TOKEN_STORAGE_DB",
    default="/app/data/tokens.db",
    show_default=True,
    help="Path to token storage database (can also use TOKEN_STORAGE_DB env var)",
)
@click.option(
    "--revision",
    "-r",
    default="head",
    show_default=True,
    help="Target revision (default: head for latest)",
)
def upgrade(database_path: str, revision: str):
    """Upgrade database to a specific revision.

    \b
    Examples:
      # Upgrade to latest version
      $ nextcloud-mcp-server db upgrade

      # Upgrade to specific revision
      $ nextcloud-mcp-server db upgrade --revision 001

      # Use custom database path
      $ nextcloud-mcp-server db upgrade -d /path/to/tokens.db
    """
    from nextcloud_mcp_server.migrations import upgrade_database

    try:
        click.echo(f"Upgrading database to revision: {revision}")
        upgrade_database(database_path, revision)
        click.echo(click.style("✓ Database upgraded successfully", fg="green"))
    except Exception as e:
        click.echo(click.style(f"✗ Upgrade failed: {e}", fg="red"), err=True)
        raise click.ClickException(str(e))


@db.command()
@click.option(
    "--database-path",
    "-d",
    envvar="TOKEN_STORAGE_DB",
    default="/app/data/tokens.db",
    show_default=True,
    help="Path to token storage database",
)
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
def downgrade(database_path: str, revision: str):
    """Downgrade database to a specific revision.

    WARNING: This may result in data loss! Use with caution.

    \b
    Examples:
      # Downgrade by one version
      $ nextcloud-mcp-server db downgrade

      # Downgrade to specific revision
      $ nextcloud-mcp-server db downgrade --revision 001

      # Downgrade to base (empty database)
      $ nextcloud-mcp-server db downgrade --revision base
    """
    from nextcloud_mcp_server.migrations import downgrade_database

    try:
        click.echo(f"Downgrading database to revision: {revision}")
        downgrade_database(database_path, revision)
        click.echo(click.style("✓ Database downgraded successfully", fg="green"))
    except Exception as e:
        click.echo(click.style(f"✗ Downgrade failed: {e}", fg="red"), err=True)
        raise click.ClickException(str(e))


@db.command()
@click.option(
    "--database-path",
    "-d",
    envvar="TOKEN_STORAGE_DB",
    default="/app/data/tokens.db",
    show_default=True,
    help="Path to token storage database",
)
def current(database_path: str):
    """Show current database revision.

    \b
    Example:
      $ nextcloud-mcp-server db current
    """
    from nextcloud_mcp_server.migrations import get_current_revision

    try:
        revision = get_current_revision(database_path)
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
@click.option(
    "--database-path",
    "-d",
    envvar="TOKEN_STORAGE_DB",
    default="/app/data/tokens.db",
    show_default=True,
    help="Path to token storage database",
)
def history(database_path: str):
    """Show migration history.

    \b
    Example:
      $ nextcloud-mcp-server db history
    """
    from nextcloud_mcp_server.migrations import show_migration_history

    try:
        click.echo("Migration history:")
        show_migration_history(database_path)
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
    from nextcloud_mcp_server.migrations import create_migration

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
cli = click.Group()
cli.add_command(run)
cli.add_command(db)


if __name__ == "__main__":
    cli()
