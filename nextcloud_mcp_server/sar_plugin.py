"""SAR case export (ADR-040), registered as a plugin.

The in-tree proof of :mod:`nextcloud_mcp_server.plugins`: everything the server
knows about SAR comes through this object, including its scopes and settings.
Imports nothing heavy at module level — the SAR modules need the ``semantic``
extra and are imported only once the plugin is available.
"""

from functools import cache
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field
from starlette.routing import BaseRoute, Route

from nextcloud_mcp_server.config import plugin_setting
from nextcloud_mcp_server.features import gateway_v1_url
from nextcloud_mcp_server.plugins import Plugin


class SarSettings(BaseModel):
    """SAR's settings, from ``SAR_ENABLED``, ``NER_MODEL``, ... (env or
    settings file)."""

    # Off by default: a deployment opts in, since only some need it. Also
    # requires vector sync and EMBEDDING_GATEWAY_URL (names are detected by the
    # gateway's ``POST /v1/ner``).
    sar_enabled: bool = False
    # NER model, addressed the gateway way (``<provider>/<model>``).
    ner_model: str = "local/urchade/gliner_multi_pii-v1"
    # Per-request budget. Export runs in the background, so this only needs to
    # cover one batch on the slowest backend (CPU GLiNER: ~570 chars/s). 0 would
    # give httpx no time budget: every NER call would time out.
    ner_timeout_seconds: float = Field(120.0, gt=0)
    # Texts (of up to 2,000 chars) per /v1/ner request. Small for CPU GLiNER,
    # which must finish a batch inside the gateway's own upstream timeout; raise
    # it (e.g. 32) on a GPU backend.
    ner_batch_size: int = Field(8, ge=1)
    # Minimum model confidence for a span to count as a person. Lower raises
    # recall at the cost of over-redaction, which is the safe direction for a
    # disclosure; 0.5 is GLiNER's customary operating point.
    ner_threshold: float = Field(0.5, gt=0, le=1)


@cache
def sar_settings() -> SarSettings:
    """SAR's settings, read and validated once per process."""
    values = {name: plugin_setting(name.upper()) for name in SarSettings.model_fields}
    return SarSettings.model_validate(
        {k: v for k, v in values.items() if v is not None}
    )


def ner_endpoint(settings: Any) -> str | None:
    """``<gateway>/v1/ner``, or ``None`` without a gateway."""
    base = gateway_v1_url(settings)
    return f"{base}/ner" if base else None


def sar_available(settings: Any) -> bool:
    """Whether SAR cases are served (ADR-040): the deployment opted in with
    ``SAR_ENABLED``, and has what they need (the index to search and read, and
    the embedding gateway to detect names).

    Raises:
        ValueError: SAR is enabled without them, or its settings are invalid.
            Opting in without either would advertise nothing and look like the
            feature is broken, so startup fails instead.
    """
    if not sar_settings().sar_enabled:
        return False
    if not (settings.vector_sync_enabled and ner_endpoint(settings)):
        raise ValueError(
            "SAR_ENABLED requires semantic search (ENABLE_SEMANTIC_SEARCH) and "
            "EMBEDDING_GATEWAY_URL (names are detected through its /v1/ner)"
        )
    return True


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
    # Reading cases, and changing them, searching for them and exporting
    # redacted archives. Their own scopes because exporting personal data about
    # someone is a distinct grant from reading files.
    scopes=frozenset({"sar.read", "sar.write"}),
)
