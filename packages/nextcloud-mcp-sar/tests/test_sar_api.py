"""/api/v1/sar/cases (ADR-040), the surface Astrolabe consumes.

Drives the real Starlette handlers, so the HTTP contract (routes, status codes,
error shape, validation) is pinned; the case operations themselves are covered
in test_sar_case.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nextcloud_mcp_sar import api
from nextcloud_mcp_sar.export import ExportError
from nextcloud_mcp_sar.models import (
    SarCase,
    SarCaseListResponse,
    SarCaseResponse,
)
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit

_MOD = "nextcloud_mcp_sar.api"
# authenticate() validates the token through here.
_AUTH = "nextcloud_mcp_server.api.management"
CASES = "/api/v1/sar/cases"
# What validate_token_and_get_user returns for a token with the SAR scopes
# (and semantic.read, which a case search also needs).
SAR_TOKEN = ("dpo", {"scopes": ["sar.read", "sar.write", "semantic.read"]})


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
            Route(case + "/search", api.search_sar_case, methods=["POST"]),
        ]
    )
    return TestClient(app)


@pytest.fixture
def nc():
    """Authenticated as "dpo", with a background client per request."""
    client = MagicMock(username="dpo", close=AsyncMock())
    with (
        patch(
            f"{_AUTH}.validate_token_and_get_user", AsyncMock(return_value=SAR_TOKEN)
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
        patch(f"{_MOD}.background_task_group", return_value="tg"),
    ):
        response = _client().post(f"{CASES}/101/exports", json={})
    assert response.status_code == 202
    request_nc, job_nc, ner, tg, case_id, folder = export.call_args.args
    assert (ner, tg, case_id, folder) == ("ner", "tg", 101, None)
    assert request_nc is nc and job_nc is nc  # both from background_client


def test_unauthenticated_is_401():
    with patch(
        f"{_AUTH}.validate_token_and_get_user",
        AsyncMock(side_effect=ValueError("Missing Authorization header")),
    ):
        response = _client().get(CASES)
    assert response.status_code == 401


def test_not_provisioned_is_403():
    with (
        patch(
            f"{_AUTH}.validate_token_and_get_user", AsyncMock(return_value=SAR_TOKEN)
        ),
        patch(
            f"{_MOD}.background_client",
            AsyncMock(side_effect=ExportError("needs background access", 403)),
        ),
    ):
        response = _client().get(CASES)
    assert response.status_code == 403


def _token(*scopes: str):
    return patch(
        f"{_AUTH}.validate_token_and_get_user",
        AsyncMock(return_value=("dpo", {"scopes": list(scopes)})),
    )


def test_read_scope_reads_but_cannot_change_cases():
    """sar.read lists and gets; everything else needs sar.write."""
    listed = AsyncMock(return_value=SarCaseListResponse(cases=[]))
    create = AsyncMock()
    client = MagicMock(username="dpo", close=AsyncMock())
    with (
        _token("sar.read", "files.read"),
        patch(f"{_MOD}.background_client", AsyncMock(return_value=client)),
        patch(f"{_MOD}.list_cases", listed),
        patch(f"{_MOD}.create_case", create),
    ):
        assert _client().get(CASES).status_code == 200
        response = _client().post(
            CASES, json={"folder": "/Team", "name": "SAR-1", "subject": ["Jane Doe"]}
        )
    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"
    create.assert_not_called()


def test_without_sar_scopes_nothing_is_served():
    with _token("files.read", "files.write", "semantic.read"):
        assert _client().get(CASES).status_code == 403
        assert _client().get(CASES + "/101").status_code == 403


def test_case_search_without_sar_write_runs_no_search():
    ran = AsyncMock()
    with (
        _token("sar.read", "semantic.read"),
        patch(f"{_MOD}.unified_search", ran),
    ):
        response = _client().post(CASES + "/101/search", json={"query": "q"})
    assert response.status_code == 403
    ran.assert_not_called()


def test_case_search_needs_semantic_read_like_the_mcp_tool():
    ran = AsyncMock()
    with (
        _token("sar.read", "sar.write"),
        patch(f"{_MOD}.unified_search", ran),
    ):
        response = _client().post(CASES + "/101/search", json={"query": "q"})
    assert response.status_code == 403
    assert "semantic.read" in response.json()["message"]
    ran.assert_not_called()


def _search_returns(status: int = 200, body: dict | None = None):
    """Stand in for the unified search handler, reading the body as it does."""

    async def search(request):
        await request.json()
        return JSONResponse(body or {"results": [], "total_found": 3}, status)

    return patch(f"{_MOD}.unified_search", search)


FILTERED = {
    "query": "grievance",
    "algorithm": "hybrid",
    "doc_types": ["file"],
    "path_prefixes": ["/HR/Conduct"],
    "modified_after": "2023-01-01T00:00:00Z",
    "granularity": "document",
    "limit": 20,
}


def test_search_returns_results_and_logs_query_with_its_filters(nc):
    change = AsyncMock(return_value=_case_response())
    with _search_returns(), patch(f"{_MOD}.change_items", change):
        response = _client().post(CASES + "/101/search", json=FILTERED)
    assert response.status_code == 200, response.text
    assert response.json()["total_found"] == 3
    (query,) = change.call_args.args[2].queries
    assert (query.text, query.hits) == ("grievance", 3)
    assert query.filters.path_prefixes == ["/HR/Conduct"]
    assert query.filters.doc_types == ["file"]
    assert query.filters.granularity == "document"


def test_search_logs_the_folders_the_search_used(nc):
    """unified_search keeps the first MAX_PATH_PREFIXES folders; so does the
    log, instead of refusing the entry after the search has run."""
    from nextcloud_mcp_server.search.access_filter import (  # noqa: PLC0415
        MAX_PATH_PREFIXES,
    )

    folders = [f"/F{i}" for i in range(MAX_PATH_PREFIXES + 5)]
    change = AsyncMock(return_value=_case_response())
    with _search_returns(), patch(f"{_MOD}.change_items", change):
        response = _client().post(
            CASES + "/101/search", json={**FILTERED, "path_prefixes": folders}
        )
    assert response.status_code == 200, response.text
    (query,) = change.call_args.args[2].queries
    assert query.filters.path_prefixes == folders[:MAX_PATH_PREFIXES]


def test_filters_the_log_cannot_hold_are_refused_before_searching(nc):
    ran = AsyncMock()
    with patch(f"{_MOD}.unified_search", ran):
        response = _client().post(
            CASES + "/101/search",
            json={**FILTERED, "doc_types": [f"t{i}" for i in range(101)]},
        )
    assert response.status_code == 400
    ran.assert_not_called()


def test_search_later_pages_are_not_logged_again(nc):
    change = AsyncMock(return_value=_case_response())
    with _search_returns(), patch(f"{_MOD}.change_items", change):
        response = _client().post(
            CASES + "/101/search", json={**FILTERED, "offset": 20}
        )
    assert response.status_code == 200
    change.assert_not_called()


def test_search_for_a_closed_case_returns_no_results(nc):
    change = AsyncMock(side_effect=ExportError("this case is closed", 409))
    with _search_returns(), patch(f"{_MOD}.change_items", change):
        response = _client().post(CASES + "/101/search", json=FILTERED)
    assert response.status_code == 409
    assert "results" not in response.json()


def test_search_errors_pass_through_unlogged(nc):
    change = AsyncMock()
    with (
        _search_returns(422, {"error": "unsupported"}),
        patch(f"{_MOD}.change_items", change),
    ):
        response = _client().post(CASES + "/101/search", json=FILTERED)
    assert response.status_code == 422
    change.assert_not_called()
