"""Management API for subject access request cases (ADR-040), used by Astrolabe.

    POST   /api/v1/sar/cases                      SarCaseCreate        -> 201 case
    GET    /api/v1/sar/cases                                           -> case list
    GET    /api/v1/sar/cases/{case_id}?offset&limit                    -> case
    PATCH  /api/v1/sar/cases/{case_id}            SarCaseUpdate        -> case
    POST   /api/v1/sar/cases/{case_id}/items      SarCaseItemsChange   -> case
    POST   /api/v1/sar/cases/{case_id}/exports    SarCaseExportRequest -> 202 case
    POST   /api/v1/sar/cases/{case_id}/search     search body          -> search results

Every operation acts as the bearer token's user, through their stored app
password, so it can only read what that user can read and write where that
user can write. Nextcloud's permissions on the case folder are the access
model; a case the user cannot see is a 404.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api.management import (
    _sanitize_error_for_client,
    validate_token_and_get_user,
)
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.models.sar import (
    SarCaseCreate,
    SarCaseExportRequest,
    SarCaseItemsChange,
    SarCaseUpdate,
    SarQueryIn,
    SarSearchFilters,
)
from nextcloud_mcp_server.redaction import get_ner_client
from nextcloud_mcp_server.sar_case import (
    change_items,
    create_case,
    export_case,
    get_case,
    list_cases,
    update_case,
)
from nextcloud_mcp_server.sar_export import ExportError, background_client

logger = logging.getLogger(__name__)

Operation = Callable[[NextcloudClient], Awaitable[BaseModel]]


def _error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


def _invalid(e: ValidationError) -> JSONResponse:
    # Field locations and messages only: the rejected input can contain
    # personal data (subject identifiers, reasons).
    details = [
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
    ]
    return _error(400, "invalid_request", "; ".join(details))


async def _body(request: Request, model: type[BaseModel]) -> Any:
    """The validated JSON body, or a 400 response."""
    try:
        return model.model_validate(await request.json())
    except ValidationError as e:
        return _invalid(e)
    except ValueError:
        return _error(400, "invalid_request", "body must be a JSON object")


def _case_id(request: Request) -> int | JSONResponse:
    try:
        return int(request.path_params["case_id"])
    except ValueError:
        return _error(400, "invalid_request", "case_id must be an integer")


async def _run(request: Request, operation: Operation, status: int = 200):
    """Authenticate, run ``operation`` as the token's user, map errors."""
    try:
        user_id, _ = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to %s: %s", request.url.path, e)
        return _error(401, "Unauthorized", _sanitize_error_for_client(e, "sar"))
    try:
        nc = await background_client(user_id)
    except ExportError as e:
        return _error(e.status, "not_provisioned", str(e))
    try:
        result = await operation(nc)
    except ExportError as e:
        return _error(e.status, "sar_case_error", str(e))
    except Exception as e:
        return _error(500, "internal_error", _sanitize_error_for_client(e, "sar"))
    finally:
        await nc.close()
    return JSONResponse(result.model_dump(mode="json"), status_code=status)


async def create_sar_case(request: Request) -> JSONResponse:
    body = await _body(request, SarCaseCreate)
    if isinstance(body, JSONResponse):
        return body
    return await _run(
        request,
        lambda nc: create_case(
            nc,
            folder=body.folder,
            name=body.name,
            subject=list(body.subject),
            description=body.description,
        ),
        status=201,
    )


async def list_sar_cases(request: Request) -> JSONResponse:
    return await _run(request, list_cases)


async def get_sar_case(request: Request) -> JSONResponse:
    case_id = _case_id(request)
    if isinstance(case_id, JSONResponse):
        return case_id
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
        limit = min(1000, max(1, int(request.query_params.get("limit", 200))))
    except ValueError:
        return _error(400, "invalid_request", "offset and limit must be integers")
    return await _run(request, lambda nc: get_case(nc, case_id, offset, limit))


async def update_sar_case(request: Request) -> JSONResponse:
    case_id = _case_id(request)
    if isinstance(case_id, JSONResponse):
        return case_id
    body = await _body(request, SarCaseUpdate)
    if isinstance(body, JSONResponse):
        return body
    return await _run(request, lambda nc: update_case(nc, case_id, body))


async def change_sar_case_items(request: Request) -> JSONResponse:
    case_id = _case_id(request)
    if isinstance(case_id, JSONResponse):
        return case_id
    body = await _body(request, SarCaseItemsChange)
    if isinstance(body, JSONResponse):
        return body
    return await _run(request, lambda nc: change_items(nc, case_id, body))


async def export_sar_case(request: Request) -> JSONResponse:
    case_id = _case_id(request)
    if isinstance(case_id, JSONResponse):
        return case_id
    body = await _body(request, SarCaseExportRequest)
    if isinstance(body, JSONResponse):
        return body
    # Lazy: app imports this module to register the routes.
    from nextcloud_mcp_server.app import background_task_group  # noqa: PLC0415

    async def start(nc: NextcloudClient) -> BaseModel:
        ner = await get_ner_client(get_settings())
        # A second client for the job: `nc` is closed when this request ends.
        # export_case owns it from here on.
        background = await background_client(nc.username)
        return await export_case(
            nc, background, ner, background_task_group(), case_id, body.output_folder
        )

    return await _run(request, start, status=202)


async def search_sar_case(request: Request) -> JSONResponse:
    """Search as ``POST /api/v1/search`` does, with the same body, and log the
    query and its filters to the case.

    The search itself is the unified search handler, unchanged, so a case
    search can never accept different filters from the search page. Only the
    first page (``offset`` 0) is logged; later pages are the same search.
    """
    case_id = _case_id(request)
    if isinstance(case_id, JSONResponse):
        return case_id
    # Lazy: visualization imports the search stack.
    from nextcloud_mcp_server.api.visualization import (  # noqa: PLC0415
        unified_search,
    )

    response = await unified_search(request)
    if response.status_code != 200:
        return response
    body = await request.json()  # Starlette caches it; unified_search read it
    if not body.get("query") or body.get("offset"):
        return response
    try:
        found = json.loads(bytes(response.body))
        log = SarQueryIn(
            text=body["query"],
            hits=found.get("total_found"),
            filters=SarSearchFilters.model_validate(body),
        )
    except ValidationError as e:
        return _invalid(e)
    logged = await _run(
        request,
        lambda nc: change_items(nc, case_id, SarCaseItemsChange(queries=[log])),
    )
    # A case the user cannot change (closed, gone, not theirs) gets no results:
    # searching "for" it must leave a record or not happen.
    return response if logged.status_code == 200 else logged
