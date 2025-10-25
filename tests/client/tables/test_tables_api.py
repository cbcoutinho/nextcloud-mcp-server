import logging

import httpx
import pytest

from nextcloud_mcp_server.client.tables import TablesClient
from tests.client.conftest import (
    create_mock_error_response,
    create_mock_response,
    create_mock_table_row_ocs_response,
    create_mock_table_row_response,
    create_mock_table_schema_response,
    create_mock_tables_list_response,
)

logger = logging.getLogger(__name__)

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


async def test_tables_list_tables(mocker):
    """Test that list_tables correctly parses the API response (OCS format)."""
    mock_response = create_mock_tables_list_response(
        tables=[
            {"id": 1, "title": "Table 1"},
            {"id": 2, "title": "Table 2"},
        ]
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        TablesClient, "_make_request", return_value=mock_response
    )

    client = TablesClient(mock_client, "testuser")
    tables = await client.list_tables()

    assert isinstance(tables, list)
    assert len(tables) == 2
    assert tables[0]["id"] == 1
    assert tables[0]["title"] == "Table 1"

    mock_make_request.assert_called_once()


async def test_tables_get_schema(mocker):
    """Test that get_table_schema correctly parses the API response."""
    mock_response = create_mock_table_schema_response(
        table_id=123,
        columns=[
            {"id": 1, "title": "Name", "type": "text"},
            {"id": 2, "title": "Age", "type": "number"},
            {"id": 3, "title": "Email", "type": "text"},
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        TablesClient, "_make_request", return_value=mock_response
    )

    client = TablesClient(mock_client, "testuser")
    schema = await client.get_table_schema(table_id=123)

    assert isinstance(schema, dict)
    assert "columns" in schema
    assert len(schema["columns"]) == 3
    assert schema["columns"][0]["title"] == "Name"

    mock_make_request.assert_called_once()
    assert "/tables/123/scheme" in mock_make_request.call_args[0][1]


async def test_tables_get_rows(mocker):
    """Test that get_table_rows correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data=[
            {
                "id": 1,
                "tableId": 123,
                "data": [
                    {"columnId": 1, "value": "John"},
                    {"columnId": 2, "value": 30},
                ],
                "createdBy": "testuser",
                "createdAt": "2024-01-01T00:00:00+00:00",
                "lastEditBy": "testuser",
                "lastEditAt": "2024-01-01T00:00:00+00:00",
            },
            {
                "id": 2,
                "tableId": 123,
                "data": [
                    {"columnId": 1, "value": "Jane"},
                    {"columnId": 2, "value": 25},
                ],
                "createdBy": "testuser",
                "createdAt": "2024-01-01T00:00:00+00:00",
                "lastEditBy": "testuser",
                "lastEditAt": "2024-01-01T00:00:00+00:00",
            },
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        TablesClient, "_make_request", return_value=mock_response
    )

    client = TablesClient(mock_client, "testuser")
    rows = await client.get_table_rows(table_id=123)

    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["id"] == 1
    assert rows[0]["tableId"] == 123

    mock_make_request.assert_called_once()


async def test_tables_get_rows_with_pagination(mocker):
    """Test that get_table_rows correctly handles pagination parameters."""
    mock_response = create_mock_response(
        status_code=200,
        json_data=[
            {
                "id": 1,
                "tableId": 123,
                "data": [],
                "createdBy": "testuser",
                "createdAt": "2024-01-01T00:00:00+00:00",
                "lastEditBy": "testuser",
                "lastEditAt": "2024-01-01T00:00:00+00:00",
            },
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        TablesClient, "_make_request", return_value=mock_response
    )

    client = TablesClient(mock_client, "testuser")
    rows = await client.get_table_rows(table_id=123, limit=5, offset=10)

    assert isinstance(rows, list)

    # Verify pagination parameters were passed
    call_args = mock_make_request.call_args
    assert call_args[1]["params"]["limit"] == 5
    assert call_args[1]["params"]["offset"] == 10


async def test_tables_create_row(mocker):
    """Test that create_row correctly parses the API response (OCS format)."""
    mock_response = create_mock_table_row_ocs_response(
        row_id=456,
        table_id=123,
        data=[
            {"columnId": 1, "value": "Test Name"},
            {"columnId": 2, "value": 99},
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        TablesClient, "_make_request", return_value=mock_response
    )

    client = TablesClient(mock_client, "testuser")
    test_data = {1: "Test Name", 2: 99}
    created_row = await client.create_row(table_id=123, data=test_data)

    assert isinstance(created_row, dict)
    assert created_row["id"] == 456
    assert created_row["tableId"] == 123

    # Verify the data was transformed to string keys
    call_args = mock_make_request.call_args
    assert call_args[1]["json"]["data"]["1"] == "Test Name"
    assert call_args[1]["json"]["data"]["2"] == 99


async def test_tables_update_row(mocker):
    """Test that update_row correctly parses the API response."""
    mock_response = create_mock_table_row_response(
        row_id=456,
        table_id=123,
        data=[
            {"columnId": 1, "value": "Updated Name"},
            {"columnId": 2, "value": 100},
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        TablesClient, "_make_request", return_value=mock_response
    )

    client = TablesClient(mock_client, "testuser")
    update_data = {1: "Updated Name", 2: 100}
    updated_row = await client.update_row(row_id=456, data=update_data)

    assert isinstance(updated_row, dict)
    assert updated_row["id"] == 456

    # Verify the PUT request was made
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "PUT"
    assert "/rows/456" in call_args[0][1]


async def test_tables_delete_row(mocker):
    """Test that delete_row correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200, json_data={"message": "Row deleted"}
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        TablesClient, "_make_request", return_value=mock_response
    )

    client = TablesClient(mock_client, "testuser")
    result = await client.delete_row(row_id=456)

    assert isinstance(result, dict)

    # Verify the DELETE request was made
    call_args = mock_make_request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/rows/456" in call_args[0][1]


async def test_tables_delete_nonexistent_row(mocker):
    """Test that deleting a non-existent row raises HTTPStatusError."""
    error_response = create_mock_error_response(404, "Row not found")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(TablesClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("DELETE", "http://test.local"),
        response=error_response,
    )

    client = TablesClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.delete_row(row_id=999999999)

    assert excinfo.value.response.status_code == 404


async def test_tables_get_nonexistent_schema(mocker):
    """Test that getting schema for non-existent table raises HTTPStatusError."""
    error_response = create_mock_error_response(404, "Table not found")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(TablesClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("GET", "http://test.local"),
        response=error_response,
    )

    client = TablesClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.get_table_schema(table_id=999999999)

    assert excinfo.value.response.status_code == 404


def test_tables_transform_row_data():
    """Test the transform_row_data utility method (synchronous)."""
    # This is a pure function, no mocking needed
    client = TablesClient(None, "testuser")  # Client not used for this method

    raw_rows = [
        {
            "id": 1,
            "tableId": 123,
            "createdBy": "testuser",
            "createdAt": "2024-01-01T00:00:00+00:00",
            "lastEditBy": "testuser",
            "lastEditAt": "2024-01-01T00:00:00+00:00",
            "data": [
                {"columnId": 1, "value": "John Doe"},
                {"columnId": 2, "value": 30},
                {"columnId": 3, "value": "john@example.com"},
            ],
        },
        {
            "id": 2,
            "tableId": 123,
            "createdBy": "testuser",
            "createdAt": "2024-01-01T00:00:00+00:00",
            "lastEditBy": "testuser",
            "lastEditAt": "2024-01-01T00:00:00+00:00",
            "data": [
                {"columnId": 1, "value": "Jane Smith"},
                {"columnId": 2, "value": 25},
                {"columnId": 3, "value": "jane@example.com"},
            ],
        },
    ]

    columns = [
        {"id": 1, "title": "Name", "type": "text"},
        {"id": 2, "title": "Age", "type": "number"},
        {"id": 3, "title": "Email", "type": "text"},
    ]

    transformed = client.transform_row_data(raw_rows, columns)

    assert len(transformed) == 2
    assert transformed[0]["id"] == 1
    assert transformed[0]["data"]["Name"] == "John Doe"
    assert transformed[0]["data"]["Age"] == 30
    assert transformed[0]["data"]["Email"] == "john@example.com"

    assert transformed[1]["data"]["Name"] == "Jane Smith"
    assert transformed[1]["data"]["Age"] == 25
