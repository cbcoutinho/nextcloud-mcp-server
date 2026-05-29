"""One-shot payload backfill admin endpoint (design §10.2).

``POST /api/v1/admin/payload-backfill`` walks the collection and adds default
values for any *missing* decomposition payload keys (so existing corpora gain
them without a re-index), then upserts the collection-metadata sentinel.

Scope: this backfills the cheap, deployment-level scalar keys
(``processor_version``, ``pipeline_tier``, ``embedding_identity``) only.
``parsed_at`` is per-document state (the local processor sets it at index time),
not a deployment-level scalar, so it is intentionally not backfilled. It also
deliberately does NOT synthesize ``acl_hash`` — a correct value needs
per-document share enumeration (a separate job), and writing a placeholder
``acl_hash`` would be unsafe to pre-filter on. The query-side ACL pre-filter
therefore stays disabled until a real ACL backfill runs (see
``ACL_PREFILTER_ENABLED``).
"""

from __future__ import annotations

import logging

from qdrant_client import models
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.api.management import (
    AdminScopeRequired,
    require_admin_scope,
)
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.collection_metadata import (
    env_default_metadata,
    upsert_sentinel,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


async def handle_payload_backfill(request: Request) -> JSONResponse:
    # require_admin_scope authenticates via the OAuth token verifier; in
    # BasicAuth-only deployments there is no oauth_context, so surface a clean
    # 404 rather than a confusing 401 from the broad except below.
    if getattr(request.app.state, "oauth_context", None) is None:
        return JSONResponse(
            {"error": "admin API requires an OAuth-capable deployment"},
            status_code=404,
        )
    try:
        await require_admin_scope(request)
    except AdminScopeRequired:
        return JSONResponse({"error": "admin scope required"}, status_code=403)
    except Exception:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    settings = get_settings()
    if not settings.vector_sync_enabled:
        return JSONResponse({"error": "vector sync disabled"}, status_code=404)

    client = await get_qdrant_client()
    collection = settings.get_collection_name()
    meta = env_default_metadata(settings)

    # Deployment-level scalar defaults (safe to set only where missing).
    defaults = {
        payload_keys.PROCESSOR_VERSION: "backfill",
        payload_keys.PIPELINE_TIER: "fast",
        payload_keys.EMBEDDING_IDENTITY: meta["embedding_identity"],
    }
    applied: dict[str, str] = {}
    for key, value in defaults.items():
        try:
            await client.set_payload(
                collection_name=collection,
                payload={key: value},
                # Only points missing this key (don't clobber existing values).
                points=models.Filter(
                    must=[
                        models.IsEmptyCondition(is_empty=models.PayloadField(key=key))
                    ]
                ),
                wait=True,
            )
            applied[key] = "set-where-missing"
        except Exception as e:
            logger.warning("payload backfill failed for key %s: %s", key, e)
            applied[key] = f"error: {e}"

    # Upsert the collection-metadata sentinel so query-path metadata reads work
    # for this collection even without a control plane.
    sentinel_ok = True
    try:
        dimension = get_embedding_service().get_dimension()
        await upsert_sentinel(
            client,
            collection,
            embedding_identity=meta["embedding_identity"],
            chunking_config=meta["chunking_config"],
            dimension=dimension,
        )
    except Exception as e:
        sentinel_ok = False
        logger.warning("sentinel upsert failed during backfill: %s", e)

    return JSONResponse(
        {
            "status": "ok",
            "collection": collection,
            "keys_applied": applied,
            "sentinel_upserted": sentinel_ok,
        }
    )
