"""POST/GET /api/v1/sar/exports (ADR-040), the surface Astrolabe consumes.

Drives the real Starlette handlers so the HTTP contract (status codes, error
shape, 202 body) is pinned; the export pipeline itself is covered in
tests/unit/test_sar_export.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.sar import create_sar_export, get_sar_export
from nextcloud_mcp_server.models.sar import SarExportStatus
from nextcloud_mcp_server.sar_export import ExportError
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit

_MOD = "nextcloud_mcp_server.api.sar"

BODY = {
    "output_folder": "/Team/SAR",
    "name": "SAR-1",
    "subject": ["Jane Doe"],
    "items": [{"doc_type": "file", "doc_id": 12, "reason": "letter"}],
}


def _status(state="running") -> SarExportStatus:
    return SarExportStatus(
        state=state,
        archive_path="/Team/SAR/SAR-1.zip",
        status_path="/Team/SAR/SAR-1.status.json",
        total=1,
        processed=0,
        failed=0,
        started_at="t",
        updated_at="t",
    )


def _client() -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/v1/sar/exports", create_sar_export, methods=["POST"]),
            Route("/api/v1/sar/exports", get_sar_export, methods=["GET"]),
        ]
    )
    return TestClient(app)


@pytest.fixture
def authed():
    with patch(
        f"{_MOD}.validate_token_and_get_user",
        AsyncMock(return_value=("dpo", {})),
    ):
        yield


def test_submit_returns_202_and_status(authed):
    submit = AsyncMock(return_value=_status())
    with (
        patch(f"{_MOD}.submit_export", submit),
        patch("nextcloud_mcp_server.app.background_task_group", return_value="tg"),
    ):
        response = _client().post("/api/v1/sar/exports", json=BODY)
    assert response.status_code == 202
    assert response.json()["state"] == "running"
    user, request, task_group = submit.call_args.args
    assert user == "dpo"
    assert request.items[0].doc_id == "12"
    assert task_group == "tg"


def test_submit_maps_export_errors_to_their_status(authed):
    submit = AsyncMock(side_effect=ExportError("an export already exists", 409))
    with patch(f"{_MOD}.submit_export", submit):
        response = _client().post("/api/v1/sar/exports", json=BODY)
    assert response.status_code == 409
    assert response.json() == {
        "error": "export_error",
        "message": "an export already exists",
    }


def test_invalid_body_is_400_without_echoing_input(authed):
    body = {**BODY, "subject": [], "items": [{"doc_type": "file", "reason": "Karen"}]}
    response = _client().post("/api/v1/sar/exports", json=body)
    assert response.status_code == 400
    message = response.json()["message"]
    assert "subject" in message and "items.0.doc_id" in message
    assert "Karen" not in message


def test_non_json_body_is_400(authed):
    response = _client().post(
        "/api/v1/sar/exports",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


def test_unauthenticated_is_401():
    with patch(
        f"{_MOD}.validate_token_and_get_user",
        AsyncMock(side_effect=ValueError("Missing Authorization header")),
    ):
        response = _client().post("/api/v1/sar/exports", json=BODY)
    assert response.status_code == 401


def test_status_reads_as_the_token_user(authed):
    nc = MagicMock(close=AsyncMock())
    read = AsyncMock(return_value=_status("done"))
    with (
        patch(f"{_MOD}.resolve_background_client", AsyncMock(return_value=nc)),
        patch(f"{_MOD}.read_status", read),
    ):
        response = _client().get(
            "/api/v1/sar/exports",
            params={"output_folder": "/Team/SAR", "name": "SAR-1"},
        )
    assert response.status_code == 200
    assert response.json()["state"] == "done"
    assert read.call_args.args == (nc, "/Team/SAR", "SAR-1")
    nc.close.assert_awaited_once()


def test_status_of_unknown_export_is_404(authed):
    nc = MagicMock(close=AsyncMock())
    with (
        patch(f"{_MOD}.resolve_background_client", AsyncMock(return_value=nc)),
        patch(f"{_MOD}.read_status", AsyncMock(side_effect=ExportError("no", 404))),
    ):
        response = _client().get(
            "/api/v1/sar/exports", params={"output_folder": "/T", "name": "X"}
        )
    assert response.status_code == 404
    nc.close.assert_awaited_once()


def test_status_requires_folder_and_name(authed):
    response = _client().get("/api/v1/sar/exports", params={"name": "X"})
    assert response.status_code == 400


def test_status_without_background_access_is_403(authed):
    with patch(
        f"{_MOD}.resolve_background_client",
        AsyncMock(side_effect=NotProvisionedError("no")),
    ):
        response = _client().get(
            "/api/v1/sar/exports", params={"output_folder": "/T", "name": "X"}
        )
    assert response.status_code == 403
