"""Vector-sync admin API endpoints.

Provides the purge endpoint Astrolabe calls when an admin disables a content
source for semantic search. Consent is binding on data-at-rest, so the
already-indexed content for the disabled source's doc type(s) is deleted
globally (every owner) — see :mod:`nextcloud_mcp_server.vector.purge`.

Auth: the OAuth bearer identifies the caller (``validate_token_and_get_user``);
because the purge deletes every owner's content for a doc type, it is further
restricted to Nextcloud administrators (verified via the ``admin`` group using
the caller's app password). This is stricter than the per-user webhook routes
in :mod:`nextcloud_mcp_server.api.webhooks` precisely because the blast radius
is global.
"""

import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api._auth import get_basic_auth_for_user
from nextcloud_mcp_server.api.management import (
    _sanitize_error_for_client,
    validate_token_and_get_user,
)
from nextcloud_mcp_server.auth.scope_authorization import ProvisioningRequiredError
from nextcloud_mcp_server.client.users import UsersClient
from nextcloud_mcp_server.vector.purge import purge_doc_types

from ..http import nextcloud_httpx_client

logger = logging.getLogger(__name__)

# Upper bound on doc_types per purge request. There are only a handful of real
# indexed types; this caps a hostile/buggy caller's fan-out of count+delete
# calls without constraining legitimate use.
_MAX_PURGE_DOC_TYPES = 64


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": "Bad request", "message": message}, status_code=400)


async def purge_doc_types_route(request: Request) -> JSONResponse:
    """POST /api/v1/vector-sync/purge — delete indexed vectors by doc type.

    Request body::

        {"doc_types": ["file", "note"]}

    Returns ``{"purged": {doc_type: deleted_count}}``. Admin-only.

    Requires OAuth bearer token for authentication.
    """
    try:
        user_id, _ = await validate_token_and_get_user(request)
    except Exception as e:
        logger.warning("Unauthorized access to /api/v1/vector-sync/purge: %s", e)
        return JSONResponse(
            {
                "error": "Unauthorized",
                "message": _sanitize_error_for_client(e, "purge_doc_types"),
            },
            status_code=401,
        )

    try:
        body = await request.json()
    except Exception as e:
        logger.warning("Purge payload was not valid JSON: %s", e)
        return _bad_request("invalid JSON")

    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object")

    raw = body.get("doc_types")
    if raw is None:
        return _bad_request("doc_types is required")
    if not isinstance(raw, list) or not all(isinstance(d, str) for d in raw):
        return _bad_request("doc_types must be a list of strings")
    doc_types = [d for d in raw if d]
    # No whitelist against INDEXED_DOC_TYPES on purpose: an unknown type yields a
    # zero-match Qdrant filter (harmless no-op), and the canonical set lives with
    # the indexer — the route shouldn't need a server update to purge a new type.
    # Bound the batch: there are only a handful of real indexed types, so a huge
    # list is abuse — cap it rather than fan out unbounded count+delete calls.
    if len(doc_types) > _MAX_PURGE_DOC_TYPES:
        return _bad_request(f"doc_types exceeds the maximum of {_MAX_PURGE_DOC_TYPES}")

    try:
        username, app_password = await get_basic_auth_for_user(user_id)

        oauth_ctx = request.app.state.oauth_context
        nextcloud_host = oauth_ctx.get("config", {}).get("nextcloud_host", "")
        if not nextcloud_host:
            raise ValueError("Nextcloud host not configured")

        # Verify admin via the caller's own app password before any deletion —
        # enforced even for an empty (no-op) request, since this is a
        # destructive admin route.
        async with nextcloud_httpx_client(
            base_url=nextcloud_host,
            auth=httpx.BasicAuth(username, app_password),
            timeout=30.0,
        ) as client:
            users_client = UsersClient(client, username)
            user_groups = await users_client.get_user_groups(username)
            if "admin" not in user_groups:
                logger.warning("Non-admin user %s attempted vector-sync purge", user_id)
                return JSONResponse(
                    {
                        "error": "Forbidden",
                        "message": "Administrator privileges required",
                    },
                    status_code=403,
                )

        if not doc_types:
            return JSONResponse({"purged": {}})

        purged = await purge_doc_types(doc_types)
        # Surface a partial-failure signal so Astrolabe knows which types were
        # NOT purged (consent not yet enforced for them) — the scanner backstop
        # still catches these, but the caller shouldn't assume full success.
        failed = [dt for dt in dict.fromkeys(doc_types) if dt not in purged]
        resp: dict = {"purged": purged}
        if failed:
            resp["failed"] = failed
        logger.info(
            "Vector-sync purge by admin %s: purged=%s failed=%s",
            user_id,
            purged,
            failed,
        )
        return JSONResponse(resp)

    except ProvisioningRequiredError as e:
        logger.info("Provisioning required for user %s: %s", user_id, e)
        return JSONResponse(
            {"error": "Provisioning required", "message": str(e)},
            status_code=428,
        )
    except Exception as e:
        logger.exception("Error purging doc types for user %s", user_id)
        return JSONResponse(
            {
                "error": "Internal error",
                "message": _sanitize_error_for_client(e, "purge_doc_types"),
            },
            status_code=500,
        )
