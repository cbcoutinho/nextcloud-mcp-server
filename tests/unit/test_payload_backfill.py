"""Admin payload-backfill endpoint (design §10.2)."""

import json

from nextcloud_mcp_server.api.management import AdminScopeRequired
from nextcloud_mcp_server.config import Settings


def _request(mocker):
    return mocker.MagicMock()


async def test_backfill_requires_admin_scope(mocker):
    from nextcloud_mcp_server.admin import payload_backfill as mod

    mocker.patch.object(
        mod, "require_admin_scope", side_effect=AdminScopeRequired("nope")
    )
    resp = await mod.handle_payload_backfill(_request(mocker))
    assert resp.status_code == 403


async def test_backfill_unauthorized_on_auth_error(mocker):
    from nextcloud_mcp_server.admin import payload_backfill as mod

    mocker.patch.object(mod, "require_admin_scope", side_effect=ValueError("no token"))
    resp = await mod.handle_payload_backfill(_request(mocker))
    assert resp.status_code == 401


async def test_backfill_happy_path_sets_keys_and_sentinel(mocker):
    from nextcloud_mcp_server.admin import payload_backfill as mod

    mocker.patch.object(mod, "require_admin_scope", return_value="admin")
    mocker.patch.object(
        mod, "get_settings", return_value=Settings(vector_sync_enabled=True)
    )
    qdrant = mocker.AsyncMock()
    mocker.patch.object(mod, "get_qdrant_client", return_value=qdrant)
    embed = mocker.MagicMock()
    embed.get_dimension.return_value = 4
    mocker.patch(
        "nextcloud_mcp_server.embedding.get_embedding_service", return_value=embed
    )
    # upsert_sentinel uses the same qdrant client; it is an AsyncMock so .upsert
    # is awaitable. Patch upsert_sentinel to assert it was invoked.
    sentinel = mocker.patch.object(mod, "upsert_sentinel", new=mocker.AsyncMock())

    resp = await mod.handle_payload_backfill(_request(mocker))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    # processor_version, pipeline_tier, embedding_identity → 3 set_payload calls.
    assert qdrant.set_payload.await_count == 3
    sentinel.assert_awaited_once()
    assert body["sentinel_upserted"] is True
