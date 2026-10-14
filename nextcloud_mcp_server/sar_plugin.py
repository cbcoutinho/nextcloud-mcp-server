"""SAR case export (ADR-040), registered as a plugin.

The in-tree proof of :mod:`nextcloud_mcp_server.plugins`: everything the server
knows about SAR comes through this object. Imports nothing heavy at module
level — the SAR modules need the ``semantic`` extra and are imported only once
the plugin is available.
"""

from mcp.server.mcpserver import MCPServer
from starlette.routing import BaseRoute, Route

from nextcloud_mcp_server.features import sar_available
from nextcloud_mcp_server.models.auth import SAR_SCOPES
from nextcloud_mcp_server.plugins import Plugin


def _register_tools(mcp: MCPServer) -> None:
    from nextcloud_mcp_server.server.sar import configure_sar_tools  # noqa: PLC0415

    configure_sar_tools(mcp)


def _routes() -> list[BaseRoute]:
    from nextcloud_mcp_server.api.sar import (  # noqa: PLC0415
        change_sar_case_items,
        create_sar_case,
        export_sar_case,
        get_sar_case,
        list_sar_cases,
        search_sar_case,
        update_sar_case,
    )

    cases = "/api/v1/sar/cases"
    case = cases + "/{case_id:int}"
    return [
        Route(cases, create_sar_case, methods=["POST"]),
        Route(cases, list_sar_cases, methods=["GET"]),
        Route(case, get_sar_case, methods=["GET"]),
        Route(case, update_sar_case, methods=["PATCH"]),
        Route(case + "/items", change_sar_case_items, methods=["POST"]),
        Route(case + "/exports", export_sar_case, methods=["POST"]),
        Route(case + "/search", search_sar_case, methods=["POST"]),
    ]


plugin = Plugin(
    name="sar",
    available=sar_available,
    register_tools=_register_tools,
    routes=_routes,
    scopes=SAR_SCOPES,
)
