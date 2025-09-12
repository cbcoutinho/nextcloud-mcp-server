import click
import logging
import uvicorn
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, AsyncExitStack
from dataclasses import dataclass

from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.config import setup_logging
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.server import (
    configure_calendar_tools,
    configure_contacts_tools,
    configure_notes_tools,
    configure_tables_tools,
    configure_webdav_tools,
    configure_deck_tools,
)


logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    client: NextcloudClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage application lifecycle with type-safe context"""
    # Initialize on startup
    logging.info("Creating Nextcloud client")
    client = NextcloudClient.from_env()
    logging.info("Client initialization wait complete.")
    try:
        yield AppContext(client=client)
    finally:
        # Cleanup on shutdown
        await client.close()


async def heartbeat(request):
    return JSONResponse({"status": "ok"})


def get_app(transport: str = "streamable-http", enabled_apps: list[str] | None = None):
    setup_logging()

    # Create an MCP server
    mcp = FastMCP("Nextcloud MCP", lifespan=app_lifespan)

    @mcp.resource("nc://capabilities")
    async def nc_get_capabilities():
        """Get the Nextcloud Host capabilities"""
        ctx: Context = (
            mcp.get_context()
        )  # https://github.com/modelcontextprotocol/python-sdk/issues/244
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.capabilities()

    # Define available apps and their configuration functions
    available_apps = {
        "notes": configure_notes_tools,
        "tables": configure_tables_tools,
        "webdav": configure_webdav_tools,
        "calendar": configure_calendar_tools,
        "contacts": configure_contacts_tools,
        "deck": configure_deck_tools,
    }

    # If no specific apps are specified, enable all
    if enabled_apps is None:
        enabled_apps = list(available_apps.keys())

    # Configure only the enabled apps
    for app_name in enabled_apps:
        if app_name in available_apps:
            logger.info(f"Configuring {app_name} tools")
            available_apps[app_name](mcp)
        else:
            logger.warning(
                f"Unknown app: {app_name}. Available apps: {list(available_apps.keys())}"
            )

    if transport == "sse":
        mcp.settings.sse_path = "/"
        mcp_app = mcp.sse_app()
        lifespan = None
    else:
        mcp.settings.streamable_http_path = "/"
        mcp_app = mcp.streamable_http_app()

        @asynccontextmanager
        async def lifespan(app: Starlette):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(mcp.session_manager.run())
                yield

    app = Starlette(
        routes=[
            Route("/heartbeat", endpoint=heartbeat),
            Mount("/sse" if transport == "sse" else "/mcp", app=mcp_app),
        ],
        lifespan=lifespan,
    )

    return app


@click.command()
@click.option("--host", "-h", default="127.0.0.1", show_default=True)
@click.option("--port", "-p", type=int, default=8000, show_default=True)
@click.option("--workers", "-w", type=int, default=None)
@click.option("--reload", "-r", is_flag=True)
@click.option(
    "--log-level",
    "-l",
    default="info",
    show_default=True,
    type=click.Choice(["critical", "error", "warning", "info", "debug", "trace"]),
)
@click.option(
    "--transport",
    "-t",
    default="sse",
    show_default=True,
    type=click.Choice(["sse", "streamable-http"]),
)
@click.option(
    "--enable-app",
    "-e",
    multiple=True,
    type=click.Choice(["notes", "tables", "webdav", "calendar", "contacts", "deck"]),
    help="Enable specific Nextcloud app APIs. Can be specified multiple times. If not specified, all apps are enabled.",
)
def run(
    host: str,
    port: int,
    workers: int,
    reload: bool,
    log_level: str,
    transport: str,
    enable_app: tuple[str, ...],
):
    enabled_apps = list(enable_app) if enable_app else None

    if reload or workers:
        app = "nextcloud_mcp_server.app:get_app"
        factory = True
    else:
        app = get_app(transport=transport, enabled_apps=enabled_apps)
        factory = False

    uvicorn.run(
        app=app,
        factory=factory,
        host=host,
        port=port,
        reload=reload,
        workers=workers,
        log_level=log_level,
    )


if __name__ == "__main__":
    run()
