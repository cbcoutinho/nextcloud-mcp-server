"""End-to-end coverage of the Tables MCP tools against a real stack.

GH #1664: the Tables v1 routes (``get_schema`` / ``read_table`` / ``update_row``
/ ``delete_row``) are ``#[CORS]`` app routes. Sent without ``OCS-APIRequest``,
Nextcloud 33's ``CORSMiddleware`` re-authenticates the request and Tables
answers an empty 404 -- even for a table the user owns. ``list_tables`` and
``insert_row`` use OCS v2 and were never affected, so the round trip below
deliberately goes through every v1 route.
"""

import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.client.ocs import OCS_REQUEST_HEADERS

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration

_TABLES_OCS = "/ocs/v2.php/apps/tables/api/2"


def _text(result) -> str:
    assert result.is_error is False, f"MCP tool call failed: {result.content}"
    return "".join(getattr(block, "text", "") for block in result.content)


@pytest.fixture
async def temporary_table(nc_client: NextcloudClient):
    """A table owned by the test user, plus a text column id; deleted afterwards.

    Built from Tables' ``todo`` template, which ships text columns -- creating a
    column via ``POST /api/2/columns/text`` 500s on Tables 2.3.2.
    """
    http = nc_client._client
    headers = dict(OCS_REQUEST_HEADERS)
    response = await http.post(
        f"{_TABLES_OCS}/tables",
        json={"title": f"MCP Test Table {uuid.uuid4().hex[:8]}", "template": "todo"},
        headers=headers,
    )
    response.raise_for_status()
    table_id = response.json()["ocs"]["data"]["id"]
    try:
        response = await http.get(
            f"{_TABLES_OCS}/columns/table/{table_id}", headers=headers
        )
        response.raise_for_status()
        columns = response.json()["ocs"]["data"]
        column_id = next(c["id"] for c in columns if c["type"] == "text")
        yield table_id, column_id
    finally:
        await http.delete(f"{_TABLES_OCS}/tables/{table_id}", headers=headers)


async def test_mcp_tables_v1_round_trip(nc_mcp_client: ClientSession, temporary_table):
    """Schema, read, update and delete all succeed on an owned table (#1664)."""
    table_id, column_id = temporary_table
    value = f"row-{uuid.uuid4().hex[:8]}"

    schema = _text(
        await nc_mcp_client.call_tool("nc_tables_get_schema", {"table_id": table_id})
    )
    assert "Task" in schema

    inserted = _text(
        await nc_mcp_client.call_tool(
            "nc_tables_insert_row",
            {"table_id": table_id, "data": {str(column_id): value}},
        )
    )
    row_id = json.loads(inserted)["id"]

    assert value in _text(
        await nc_mcp_client.call_tool("nc_tables_read_table", {"table_id": table_id})
    )

    updated_value = f"{value}-updated"
    _text(
        await nc_mcp_client.call_tool(
            "nc_tables_update_row",
            {"row_id": row_id, "data": {str(column_id): updated_value}},
        )
    )
    assert updated_value in _text(
        await nc_mcp_client.call_tool("nc_tables_read_table", {"table_id": table_id})
    )

    _text(await nc_mcp_client.call_tool("nc_tables_delete_row", {"row_id": row_id}))
    assert updated_value not in _text(
        await nc_mcp_client.call_tool("nc_tables_read_table", {"table_id": table_id})
    )
