"""Management API for SAR export archives (ADR-040), used by the Astrolabe app.

    POST /api/v1/sar/exports   body: SarExportRequest -> 202 + SarExportStatus
    GET  /api/v1/sar/exports?output_folder=...&name=... -> SarExportStatus

Both act as the bearer token's user, so a caller can only export what that user
can access and write where that user can write.
"""

import logging

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api.management import (
    _sanitize_error_for_client,
    validate_token_and_get_user,
)
from nextcloud_mcp_server.models.sar import SarExportRequest
from nextcloud_mcp_server.sar_export import ExportError, read_status, submit_export
from nextcloud_mcp_server.vector.oauth_sync import (
    NotProvisionedError,
    resolve_background_client,
)

logger = logging.getLogger(__name__)


def _error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


async def _authenticate(request: Request) -> str | JSONResponse:
    try:
        user_id, _ = await validate_token_and_get_user(request)
        return user_id
    except Exception as e:
        logger.warning("Unauthorized access to %s: %s", request.url.path, e)
        return _error(401, "Unauthorized", _sanitize_error_for_client(e, "sar"))


async def create_sar_export(request: Request) -> JSONResponse:
    """POST /api/v1/sar/exports: start a redacted export in the background."""
    user = await _authenticate(request)
    if isinstance(user, JSONResponse):
        return user
    try:
        body = SarExportRequest.model_validate(await request.json())
    except ValidationError as e:
        # Field locations and messages only: the rejected input can contain
        # personal data (subject identifiers, reasons).
        details = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
        return _error(400, "invalid_request", "; ".join(details))
    except ValueError:
        return _error(400, "invalid_request", "body must be a JSON object")

    # Lazy: app imports this module to register the route.
    from nextcloud_mcp_server.app import background_task_group  # noqa: PLC0415

    try:
        status = await submit_export(user, body, background_task_group())
    except ExportError as e:
        return _error(e.status, "export_error", str(e))
    except Exception as e:
        return _error(500, "internal_error", _sanitize_error_for_client(e, "sar"))
    return JSONResponse(status.model_dump(mode="json"), status_code=202)


async def get_sar_export(request: Request) -> JSONResponse:
    """GET /api/v1/sar/exports: the status of one export."""
    user = await _authenticate(request)
    if isinstance(user, JSONResponse):
        return user
    folder = request.query_params.get("output_folder")
    name = request.query_params.get("name")
    if not folder or not name:
        return _error(400, "invalid_request", "output_folder and name are required")
    try:
        nc = await resolve_background_client(user)
    except NotProvisionedError:
        return _error(403, "not_provisioned", "background access is not provisioned")
    try:
        status = await read_status(nc, folder, name)
    except ExportError as e:
        return _error(e.status, "export_error", str(e))
    except Exception as e:
        return _error(500, "internal_error", _sanitize_error_for_client(e, "sar"))
    finally:
        await nc.close()
    return JSONResponse(status.model_dump(mode="json"))
