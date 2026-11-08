"""GH #1796: read a view, read a single row, and project columns in the Tables tools."""

from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult

from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.server.tables import configure_tables_tools

pytestmark = pytest.mark.unit


def _row(row_id: int) -> dict:
    return {
        "id": row_id,
        "tableId": 7,
        "data": [
            {"columnId": 1, "value": f"a{row_id}"},
            {"columnId": 2, "value": row_id},
            {"columnId": 3, "value": "noise"},
        ],
    }


@pytest.fixture
def client(mocker):
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    client = mocker.MagicMock()
    client.tables.get_table_rows = mocker.AsyncMock(return_value=[_row(1), _row(2)])
    client.tables.get_view_rows = mocker.AsyncMock(return_value=[_row(2)])
    client.tables.get_row = mocker.AsyncMock(return_value=_row(5))
    mocker.patch(
        "nextcloud_mcp_server.server.tables.get_client",
        mocker.AsyncMock(return_value=client),
    )
    return client


async def _call(name: str, args: dict) -> dict:
    mcp = NextcloudMCPServer("test")
    configure_tables_tools(mcp)
    result = await mcp.call_tool(name, args)
    blocks = result.content if isinstance(result, CallToolResult) else result[0]
    assert len(blocks) == 1
    return json.loads(blocks[0].text)


async def test_read_table_projects_columns(client):
    payload = await _call("nc_tables_read_table", {"table_id": 7, "columns": [2, 1]})
    assert [r["data"] for r in payload["rows"]] == [
        {"1": "a1", "2": 1},
        {"1": "a2", "2": 2},
    ]


async def test_read_table_without_columns_keeps_all(client):
    payload = await _call("nc_tables_read_table", {"table_id": 7})
    assert payload["rows"][0]["data"] == {"1": "a1", "2": 1, "3": "noise"}


async def test_read_view(client):
    payload = await _call(
        "nc_tables_read_view",
        {"view_id": 9, "limit": 10, "offset": 20, "columns": [3]},
    )
    client.tables.get_view_rows.assert_awaited_once_with(9, 10, 20)
    assert payload["view_id"] == 9
    assert payload["rows"] == [
        {**payload["rows"][0], "id": 2, "data": {"3": "noise"}},
    ]


async def test_get_row(client):
    payload = await _call("nc_tables_get_row", {"row_id": 5, "columns": [1]})
    client.tables.get_row.assert_awaited_once_with(5)
    assert payload["row"]["id"] == 5
    assert payload["row"]["data"] == {"1": "a5"}


async def test_unknown_column_yields_empty_data(client):
    payload = await _call("nc_tables_get_row", {"row_id": 5, "columns": [99]})
    assert payload["row"]["data"] == {}
