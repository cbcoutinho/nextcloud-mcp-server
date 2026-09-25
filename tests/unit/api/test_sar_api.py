"""/api/v1/sar/cases (ADR-040), the surface Astrolabe consumes.

Drives the real Starlette handlers, so the HTTP contract (routes, status codes,
error shape, validation) is pinned; the case operations themselves are covered
in tests/unit/test_sar_case.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api import sar as api
from nextcloud_mcp_server.models.sar import (
    SarCase,
    SarCaseListResponse,
    SarCaseResponse,
)
from nextcloud_mcp_server.sar_export import ExportError

pytestmark = pytest.mark.unit

_MOD = "nextcloud_mcp_server.api.sar"
CASES = "/api/v1/sar/cases"


def _case_response(state="open") -> SarCaseResponse:
    case = SarCase(
        name="SAR-1",
        state=state,
        created_by="dpo",
        created_at="t",
        updated_at="t",
        subject=["Jane Doe"],
    )
    return SarCaseResponse(
        case_id=101, path="/Team/SAR-1/sar-case.json", case=case, items_total=0
    )


def _client() -> TestClient:
    case = CASES + "/{case_id:int}"
    app = Starlette(
        routes=[
            Route(CASES, api.create_sar_case, methods=["POST"]),
            Route(CASES, api.list_sar_cases, methods=["GET"]),
            Route(case, api.get_sar_case, methods=["GET"]),
            Route(case, api.update_sar_case, methods=["PATCH"]),
            Route(case + "/items", api.change_sar_case_items, methods=["POST"]),
            Route(case + "/exports", api.export_sar_case, methods=["POST"]),
        ]
    )
    return TestClient(app)


@pytest.fixture
def nc():
    """Authenticated as "dpo", with a background client per request."""
    client = MagicMock(username="dpo", close=AsyncMock())
    with (
        patch(
            f"{_MOD}.validate_token_and_get_user", AsyncMock(return_value=("dpo", {}))
        ),
        patch(f"{_MOD}.background_client", AsyncMock(return_value=client)),
    ):
        yield client


def test_create_returns_201_and_closes_client(nc):
    create = AsyncMock(return_value=_case_response())
    with patch(f"{_MOD}.create_case", create):
        response = _client().post(
            CASES, json={"folder": "/Team", "name": "SAR-1", "subject": ["Jane Doe"]}
        )
    assert response.status_code == 201, response.text
    assert response.json()["case_id"] == 101
    assert create.call_args.kwargs == {
        "folder": "/Team",
        "name": "SAR-1",
        "subject": ["Jane Doe"],
        "description": "",
    }
    nc.close.assert_awaited_once()


def test_invalid_body_is_400_without_echoing_input(nc):
    response = _client().post(
        CASES, json={"folder": "/Team", "name": "SAR-1", "subject": [], "x": "Karen"}
    )
    assert response.status_code == 400
    assert "subject" in response.json()["message"]
    assert "Karen" not in response.text


def test_list(nc):
    with patch(
        f"{_MOD}.list_cases", AsyncMock(return_value=SarCaseListResponse(cases=[]))
    ):
        response = _client().get(CASES)
    assert response.status_code == 200
    assert response.json()["cases"] == []


def test_get_passes_paging(nc):
    get = AsyncMock(return_value=_case_response())
    with patch(f"{_MOD}.get_case", get):
        response = _client().get(f"{CASES}/101", params={"offset": 5, "limit": 50})
    assert response.status_code == 200
    assert get.call_args.args[1:] == (101, 5, 50)


def test_case_errors_keep_their_status(nc):
    with patch(
        f"{_MOD}.get_case", AsyncMock(side_effect=ExportError("no SAR case 7", 404))
    ):
        response = _client().get(f"{CASES}/7")
    assert response.status_code == 404
    assert response.json() == {"error": "sar_case_error", "message": "no SAR case 7"}
    nc.close.assert_awaited_once()


def test_non_integer_case_id_is_not_routed(nc):
    assert _client().get(f"{CASES}/abc").status_code == 404


def test_update_validates_state(nc):
    response = _client().patch(f"{CASES}/101", json={"state": "exporting"})
    assert response.status_code == 400
    update = AsyncMock(return_value=_case_response("closed"))
    with patch(f"{_MOD}.update_case", update):
        response = _client().patch(f"{CASES}/101", json={"state": "closed"})
    assert response.status_code == 200
    assert update.call_args.args[2].state == "closed"


def test_items(nc):
    change = AsyncMock(return_value=_case_response())
    body = {
        "add": [{"doc_type": "note", "doc_id": 12, "reason": "r"}],
        "remove": [{"doc_type": "file", "doc_id": "9"}],
        "queries": [{"text": "jane doe", "hits": 3}],
    }
    with patch(f"{_MOD}.change_items", change):
        response = _client().post(f"{CASES}/101/items", json=body)
    assert response.status_code == 200
    request = change.call_args.args[2]
    assert request.add[0].doc_id == "12"
    assert request.queries[0].hits == 3


def test_export_returns_202_with_its_own_job_client(nc):
    export = AsyncMock(return_value=_case_response("exporting"))
    with (
        patch(f"{_MOD}.export_case", export),
        patch(f"{_MOD}.get_ner_client", AsyncMock(return_value="ner")),
        patch("nextcloud_mcp_server.app.background_task_group", return_value="tg"),
    ):
        response = _client().post(f"{CASES}/101/exports", json={})
    assert response.status_code == 202
    request_nc, job_nc, ner, tg, case_id, folder = export.call_args.args
    assert (ner, tg, case_id, folder) == ("ner", "tg", 101, None)
    assert request_nc is nc and job_nc is nc  # both from background_client


def test_unauthenticated_is_401():
    with patch(
        f"{_MOD}.validate_token_and_get_user",
        AsyncMock(side_effect=ValueError("Missing Authorization header")),
    ):
        response = _client().get(CASES)
    assert response.status_code == 401


def test_not_provisioned_is_403():
    with (
        patch(
            f"{_MOD}.validate_token_and_get_user", AsyncMock(return_value=("dpo", {}))
        ),
        patch(
            f"{_MOD}.background_client",
            AsyncMock(side_effect=ExportError("needs background access", 403)),
        ),
    ):
        response = _client().get(CASES)
    assert response.status_code == 403
