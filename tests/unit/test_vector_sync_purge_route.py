"""Unit tests for the /api/v1/vector-sync/purge admin route.

The purge is global and destructive (deletes every owner's content for a doc
type), so the route must: authenticate the bearer, restrict to Nextcloud
admins, validate the body, and only then delegate to the global purge.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.vector_sync import purge_doc_types_route
from nextcloud_mcp_server.auth.scope_authorization import ProvisioningRequiredError

pytestmark = pytest.mark.unit


def _build_app() -> Starlette:
    app = Starlette(
        routes=[
            Route(
                "/api/v1/vector-sync/purge",
                purge_doc_types_route,
                methods=["POST"],
            )
        ]
    )
    app.state.oauth_context = {"config": {"nextcloud_host": "http://nc.test"}}
    return app


def _patch_token(mocker, user_id="admin"):
    mocker.patch(
        "nextcloud_mcp_server.api.vector_sync.validate_token_and_get_user",
        new=AsyncMock(return_value=(user_id, {"sub": user_id})),
    )


def _patch_basic_auth(mocker, username="admin"):
    mocker.patch(
        "nextcloud_mcp_server.api.vector_sync.get_basic_auth_for_user",
        new=AsyncMock(return_value=(username, "app-pwd")),
    )


def _patch_outbound_client(mocker):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    mocker.patch(
        "nextcloud_mcp_server.api.vector_sync.nextcloud_httpx_client",
        MagicMock(return_value=client),
    )
    return client


def _patch_groups(mocker, groups):
    instance = MagicMock()
    instance.get_user_groups = AsyncMock(return_value=groups)
    mocker.patch(
        "nextcloud_mcp_server.api.vector_sync.UsersClient",
        MagicMock(return_value=instance),
    )


def _patch_purge(mocker, result=None):
    return mocker.patch(
        "nextcloud_mcp_server.api.vector_sync.purge_doc_types",
        new=AsyncMock(return_value=result or {}),
    )


def test_unauthorized_when_token_invalid(mocker):
    mocker.patch(
        "nextcloud_mcp_server.api.vector_sync.validate_token_and_get_user",
        new=AsyncMock(side_effect=ValueError("bad token")),
    )
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": ["file"]})

    assert resp.status_code == 401
    purge.assert_not_called()


def test_bad_request_when_doc_types_not_list(mocker):
    _patch_token(mocker)
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": "file"})

    assert resp.status_code == 400
    purge.assert_not_called()


def test_bad_request_when_doc_types_has_non_string(mocker):
    # Covers the all(isinstance(d, str)) branch (a list with non-string items).
    _patch_token(mocker)
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": [1, 2]})

    assert resp.status_code == 400
    purge.assert_not_called()


def test_total_failure_returns_500(mocker):
    # purge_doc_types raising (total failure) hits the route's except -> 500.
    _patch_token(mocker, "admin")
    _patch_basic_auth(mocker, "admin")
    _patch_outbound_client(mocker)
    _patch_groups(mocker, ["admin"])
    mocker.patch(
        "nextcloud_mcp_server.api.vector_sync.purge_doc_types",
        new=AsyncMock(side_effect=RuntimeError("qdrant down")),
    )

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": ["file"]})

    assert resp.status_code == 500


def test_forbidden_when_not_admin(mocker):
    _patch_token(mocker, "bob")
    _patch_basic_auth(mocker, "bob")
    _patch_outbound_client(mocker)
    _patch_groups(mocker, ["users"])  # not an admin
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": ["file"]})

    assert resp.status_code == 403
    purge.assert_not_called()


def test_missing_doc_types_key_returns_400(mocker):
    _patch_token(mocker)
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={})

    assert resp.status_code == 400
    purge.assert_not_called()


def test_empty_doc_types_is_admin_gated_noop(mocker):
    # An empty (no-op) request still requires admin — this is a destructive route.
    _patch_token(mocker, "admin")
    _patch_basic_auth(mocker, "admin")
    _patch_outbound_client(mocker)
    _patch_groups(mocker, ["admin"])
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": []})

    assert resp.status_code == 200
    assert resp.json() == {"purged": {}}
    purge.assert_not_called()


def test_empty_doc_types_forbidden_for_non_admin(mocker):
    _patch_token(mocker, "bob")
    _patch_basic_auth(mocker, "bob")
    _patch_outbound_client(mocker)
    _patch_groups(mocker, ["users"])
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": []})

    assert resp.status_code == 403
    purge.assert_not_called()


def test_admin_purge_happy_path(mocker):
    _patch_token(mocker, "admin")
    _patch_basic_auth(mocker, "admin")
    _patch_outbound_client(mocker)
    _patch_groups(mocker, ["admin"])
    purge = _patch_purge(mocker, {"file": 12})

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": ["file"]})

    assert resp.status_code == 200
    assert resp.json() == {"purged": {"file": 12}}
    purge.assert_awaited_once_with(["file"])


def test_partial_failure_reports_failed_types(mocker):
    # purge_doc_types returns only the succeeded types; the route must tell the
    # caller which requested types were NOT purged.
    _patch_token(mocker, "admin")
    _patch_basic_auth(mocker, "admin")
    _patch_outbound_client(mocker)
    _patch_groups(mocker, ["admin"])
    _patch_purge(mocker, {"file": 3})  # "note" failed

    client = TestClient(_build_app())
    resp = client.post(
        "/api/v1/vector-sync/purge", json={"doc_types": ["file", "note"]}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["purged"] == {"file": 3}
    assert body["failed"] == ["note"]


def test_bad_request_when_body_not_object(mocker):
    # A valid JSON non-object (e.g. a list) must 400, not 500.
    _patch_token(mocker)
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json=[1, 2, 3])

    assert resp.status_code == 400
    purge.assert_not_called()


def test_bad_request_when_too_many_doc_types(mocker):
    _patch_token(mocker)
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post(
        "/api/v1/vector-sync/purge",
        json={"doc_types": [f"t{i}" for i in range(65)]},
    )

    assert resp.status_code == 400
    purge.assert_not_called()


def test_provisioning_required_returns_428(mocker):
    _patch_token(mocker, "admin")
    mocker.patch(
        "nextcloud_mcp_server.api.vector_sync.get_basic_auth_for_user",
        new=AsyncMock(side_effect=ProvisioningRequiredError("not provisioned")),
    )
    purge = _patch_purge(mocker)

    client = TestClient(_build_app())
    resp = client.post("/api/v1/vector-sync/purge", json={"doc_types": ["file"]})

    assert resp.status_code == 428
    purge.assert_not_called()
