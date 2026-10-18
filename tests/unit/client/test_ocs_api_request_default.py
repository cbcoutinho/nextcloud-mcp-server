"""Default OCS-APIRequest header on every BaseNextcloudClient request (#1664)."""

from types import MappingProxyType

import httpx
import pytest

from nextcloud_mcp_server.client.base import BaseNextcloudClient
from nextcloud_mcp_server.client.tables import TablesClient

pytestmark = pytest.mark.unit


class _Client(BaseNextcloudClient):
    app_name = "test"


async def test_make_request_injects_ocs_api_request_when_headers_omitted(mocker):
    """Tables-style v1 calls that forget the header still reach Nextcloud 33."""
    request = httpx.Request(
        "GET", "https://nc.example.com/index.php/apps/tables/api/1/tables/8/rows"
    )
    response = httpx.Response(200, json=[{"id": 1}], request=request)
    http_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    http_client.request = mocker.AsyncMock(return_value=response)

    client = _Client(http_client, "alice")
    result = await client._make_request("GET", "/apps/tables/api/1/tables/8/rows")

    assert result.status_code == 200
    headers = http_client.request.call_args.kwargs["headers"]
    assert headers["OCS-APIRequest"] == "true"
    # Path still goes through the /index.php prefixer (#732).
    assert http_client.request.call_args.args[1].endswith(
        "/index.php/apps/tables/api/1/tables/8/rows"
    )


async def test_make_request_preserves_caller_headers_and_keeps_ocs_flag(mocker):
    request = httpx.Request(
        "GET", "https://nc.example.com/ocs/v2.php/apps/tables/api/2/tables"
    )
    response = httpx.Response(200, json={"ocs": {"data": []}}, request=request)
    http_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    http_client.request = mocker.AsyncMock(return_value=response)

    client = _Client(http_client, "alice")
    await client._make_request(
        "GET",
        "/ocs/v2.php/apps/tables/api/2/tables",
        headers={"Accept": "application/json", "X-Custom": "1"},
    )

    headers = http_client.request.call_args.kwargs["headers"]
    assert headers["OCS-APIRequest"] == "true"
    assert headers["Accept"] == "application/json"
    assert headers["X-Custom"] == "1"


async def test_make_request_caller_can_override_ocs_flag(mocker):
    """Explicit caller value wins — default must not clobber intentional overrides."""
    request = httpx.Request("GET", "https://nc.example.com/x")
    response = httpx.Response(200, json={}, request=request)
    http_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    http_client.request = mocker.AsyncMock(return_value=response)

    client = _Client(http_client, "alice")
    await client._make_request("GET", "/x", headers={"OCS-APIRequest": "false"})

    headers = http_client.request.call_args.kwargs["headers"]
    assert headers["OCS-APIRequest"] == "false"


async def test_make_request_accepts_mapping_proxy_headers(mocker):
    """Shared OCS_REQUEST_HEADERS is a MappingProxyType; merging must not mutate it."""
    from nextcloud_mcp_server.client.ocs import OCS_REQUEST_HEADERS

    request = httpx.Request("GET", "https://nc.example.com/ocs/v2.php/x")
    response = httpx.Response(200, json={}, request=request)
    http_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    http_client.request = mocker.AsyncMock(return_value=response)

    client = _Client(http_client, "alice")
    await client._make_request("GET", "/ocs/v2.php/x", headers=OCS_REQUEST_HEADERS)

    headers = http_client.request.call_args.kwargs["headers"]
    assert headers["OCS-APIRequest"] == "true"
    assert headers["Accept"] == "application/json"
    assert isinstance(OCS_REQUEST_HEADERS, MappingProxyType)


async def test_tables_get_table_rows_sends_ocs_header_through_make_request(mocker):
    """End-to-end: TablesClient.get_table_rows reaches httpx with the header."""
    request = httpx.Request(
        "GET", "https://nc.example.com/index.php/apps/tables/api/1/tables/8/rows"
    )
    response = httpx.Response(200, json=[{"id": 8}], request=request)
    http_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    http_client.request = mocker.AsyncMock(return_value=response)

    client = TablesClient(http_client, "alice")
    rows = await client.get_table_rows(table_id=8)

    assert rows == [{"id": 8}]
    headers = http_client.request.call_args.kwargs["headers"]
    assert headers["OCS-APIRequest"] == "true"


async def test_tables_get_table_schema_sends_ocs_header_through_make_request(mocker):
    request = httpx.Request(
        "GET", "https://nc.example.com/index.php/apps/tables/api/1/tables/8/scheme"
    )
    response = httpx.Response(200, json={"columns": []}, request=request)
    http_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    http_client.request = mocker.AsyncMock(return_value=response)

    client = TablesClient(http_client, "alice")
    schema = await client.get_table_schema(table_id=8)

    assert schema == {"columns": []}
    headers = http_client.request.call_args.kwargs["headers"]
    assert headers["OCS-APIRequest"] == "true"
