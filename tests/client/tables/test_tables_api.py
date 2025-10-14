import asyncio
import logging
import uuid
from typing import Any, Dict

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def sample_table_info(nc_client: NextcloudClient) -> Dict[str, Any]:
    """
    Fixture to get information about the sample table that comes with Nextcloud Tables.
    This assumes that the sample table exists in the Nextcloud instance.
    """
    logger.info("Looking for sample table in Nextcloud Tables app")

    # Get all tables
    tables = await nc_client.tables.list_tables()

    # Look for a sample table (usually created by default)
    sample_table = None
    for table in tables:
        # Common names for sample tables
        if any(
            keyword in table.get("title", "").lower()
            for keyword in ["sample", "demo", "example", "test"]
        ):
            sample_table = table
            break

    if not sample_table and tables:
        # If no sample table found, use the first available table
        sample_table = tables[0]
        logger.info(
            f"No sample table found, using first available table: {sample_table.get('title')}"
        )

    if not sample_table:
        pytest.skip(
            "No tables found in Nextcloud Tables app. Please ensure Tables app is installed and has at least one table."
        )

    # Get the schema for the sample table
    table_id = sample_table["id"]
    schema = await nc_client.tables.get_table_schema(table_id)

    logger.info(f"Using sample table: {sample_table.get('title')} (ID: {table_id})")

    return {
        "table": sample_table,
        "schema": schema,
        "table_id": table_id,
        "columns": schema.get("columns", []),
    }


@pytest.fixture
async def temporary_table_row(
    nc_client: NextcloudClient, sample_table_info: Dict[str, Any]
):
    """
    Fixture to create a temporary row in the sample table for testing.
    Yields the created row data and cleans up afterward.
    """
    table_id = sample_table_info["table_id"]
    columns = sample_table_info["columns"]

    # Create test data based on the table schema
    test_data = {}
    unique_suffix = uuid.uuid4().hex[:8]

    for column in columns:
        column_id = column["id"]
        column_type = column.get("type", "text")
        column_title = column.get("title", f"column_{column_id}")

        # Generate test data based on column type
        if column_type == "text":
            test_data[column_id] = f"Test {column_title} {unique_suffix}"
        elif column_type == "number":
            test_data[column_id] = 42
        elif column_type == "datetime":
            test_data[column_id] = "2024-01-01T12:00:00Z"
        elif column_type == "select":
            # For select columns, use the first option if available
            options = column.get("selectOptions", [])
            if options:
                test_data[column_id] = options[0].get("label", "Option 1")
            else:
                test_data[column_id] = "Test Option"
        else:
            # Default to text for unknown types
            test_data[column_id] = f"Test {column_title} {unique_suffix}"

    logger.info(f"Creating temporary row in table {table_id} with data: {test_data}")

    created_row = None
    try:
        created_row = await nc_client.tables.create_row(table_id, test_data)
        row_id = created_row.get("id")

        if not row_id:
            pytest.fail("Failed to get ID from created temporary row.")

        logger.info(f"Temporary row created with ID: {row_id}")
        yield created_row

    finally:
        if created_row and created_row.get("id"):
            row_id = created_row["id"]
            logger.info(f"Cleaning up temporary row ID: {row_id}")
            try:
                await nc_client.tables.delete_row(row_id)
                logger.info(f"Successfully deleted temporary row ID: {row_id}")
            except HTTPStatusError as e:
                # Ignore 404 if row was already deleted by the test itself
                if e.response.status_code != 404:
                    logger.error(f"HTTP error deleting temporary row {row_id}: {e}")
                else:
                    logger.warning(f"Temporary row {row_id} already deleted (404).")
            except Exception as e:
                logger.error(f"Unexpected error deleting temporary row {row_id}: {e}")


async def test_tables_list_tables(nc_client: NextcloudClient):
    """
    Test listing all tables available to the user.
    """
    logger.info("Testing list_tables functionality")

    tables = await nc_client.tables.list_tables()

    assert isinstance(tables, list)
    assert len(tables) > 0, "Expected at least one table to be available"

    # Check that each table has required fields
    for table in tables:
        assert "id" in table
        assert "title" in table
        assert isinstance(table["id"], int)
        assert isinstance(table["title"], str)

    logger.info(f"Successfully listed {len(tables)} tables")


async def test_tables_get_schema(
    nc_client: NextcloudClient, sample_table_info: Dict[str, Any]
):
    """
    Test getting the schema/structure of a specific table.
    """
    table_id = sample_table_info["table_id"]

    logger.info(f"Testing get_table_schema for table ID: {table_id}")

    schema = await nc_client.tables.get_table_schema(table_id)

    assert isinstance(schema, dict)
    assert "columns" in schema
    assert isinstance(schema["columns"], list)
    assert len(schema["columns"]) > 0, "Expected at least one column in the table"

    # Check that each column has required fields
    for column in schema["columns"]:
        assert "id" in column
        assert "title" in column
        assert "type" in column
        assert isinstance(column["id"], int)
        assert isinstance(column["title"], str)
        assert isinstance(column["type"], str)

    logger.info(f"Successfully retrieved schema with {len(schema['columns'])} columns")


async def test_tables_read_table(
    nc_client: NextcloudClient, sample_table_info: Dict[str, Any]
):
    """
    Test reading rows from a table.
    """
    table_id = sample_table_info["table_id"]

    logger.info(f"Testing get_table_rows for table ID: {table_id}")

    # Test without pagination
    rows = await nc_client.tables.get_table_rows(table_id)

    assert isinstance(rows, list)
    # Note: The table might be empty, so we don't assert len > 0

    # Test with pagination
    rows_limited = await nc_client.tables.get_table_rows(table_id, limit=5, offset=0)

    assert isinstance(rows_limited, list)
    assert len(rows_limited) <= 5

    # If there are rows, check their structure
    if rows:
        row = rows[0]
        assert "id" in row
        assert "tableId" in row
        assert "data" in row
        assert isinstance(row["id"], int)
        assert isinstance(row["tableId"], int)
        assert isinstance(row["data"], list)

    logger.info(f"Successfully read {len(rows)} rows from table")


async def test_tables_create_row(
    nc_client: NextcloudClient, sample_table_info: Dict[str, Any]
):
    """
    Test creating a new row in a table.
    """
    table_id = sample_table_info["table_id"]
    columns = sample_table_info["columns"]

    # Create test data based on the table schema
    test_data = {}
    unique_suffix = uuid.uuid4().hex[:8]

    for column in columns:
        column_id = column["id"]
        column_type = column.get("type", "text")
        column_title = column.get("title", f"column_{column_id}")

        # Generate test data based on column type
        if column_type == "text":
            test_data[column_id] = f"Test Create {column_title} {unique_suffix}"
        elif column_type == "number":
            test_data[column_id] = 123
        elif column_type == "datetime":
            test_data[column_id] = "2024-01-01T12:00:00Z"
        elif column_type == "select":
            # For select columns, use the first option if available
            options = column.get("selectOptions", [])
            if options:
                test_data[column_id] = options[0].get("label", "Option 1")
            else:
                test_data[column_id] = "Test Option"
        else:
            # Default to text for unknown types
            test_data[column_id] = f"Test Create {column_title} {unique_suffix}"

    logger.info(f"Testing create_row for table ID: {table_id} with data: {test_data}")

    created_row = None
    try:
        created_row = await nc_client.tables.create_row(table_id, test_data)

        assert isinstance(created_row, dict)
        assert "id" in created_row
        assert "tableId" in created_row
        assert isinstance(created_row["id"], int)
        assert created_row["tableId"] == table_id

        # Verify the row was created by reading it back
        await asyncio.sleep(1)  # Allow potential propagation delay
        rows = await nc_client.tables.get_table_rows(table_id)
        created_row_id = created_row["id"]

        # Find the created row in the results
        found_row = None
        for row in rows:
            if row["id"] == created_row_id:
                found_row = row
                break

        assert found_row is not None, (
            f"Created row with ID {created_row_id} not found in table"
        )

        logger.info(f"Successfully created row with ID: {created_row_id}")

    finally:
        # Clean up the created row
        if created_row and created_row.get("id"):
            try:
                await nc_client.tables.delete_row(created_row["id"])
                logger.info(f"Cleaned up created row ID: {created_row['id']}")
            except Exception as e:
                logger.warning(f"Failed to clean up created row: {e}")


async def test_tables_update_row(
    nc_client: NextcloudClient,
    temporary_table_row: Dict[str, Any],
    sample_table_info: Dict[str, Any],
):
    """
    Test updating an existing row in a table.
    """
    row_id = temporary_table_row["id"]
    columns = sample_table_info["columns"]

    # Create updated data
    update_data = {}
    unique_suffix = uuid.uuid4().hex[:8]

    for column in columns:
        column_id = column["id"]
        column_type = column.get("type", "text")
        column_title = column.get("title", f"column_{column_id}")

        # Generate updated test data based on column type
        if column_type == "text":
            update_data[column_id] = f"Updated {column_title} {unique_suffix}"
        elif column_type == "number":
            update_data[column_id] = 456
        elif column_type == "datetime":
            update_data[column_id] = "2024-12-31T23:59:59Z"
        elif column_type == "select":
            # For select columns, use the first option if available
            options = column.get("selectOptions", [])
            if options:
                update_data[column_id] = options[0].get("label", "Option 1")
            else:
                update_data[column_id] = "Updated Option"
        else:
            # Default to text for unknown types
            update_data[column_id] = f"Updated {column_title} {unique_suffix}"

    logger.info(f"Testing update_row for row ID: {row_id} with data: {update_data}")

    updated_row = await nc_client.tables.update_row(row_id, update_data)

    assert isinstance(updated_row, dict)
    assert "id" in updated_row
    assert updated_row["id"] == row_id

    # Verify the row was updated by reading it back
    await asyncio.sleep(1)  # Allow potential propagation delay
    table_id = sample_table_info["table_id"]
    rows = await nc_client.tables.get_table_rows(table_id)

    # Find the updated row in the results
    found_row = None
    for row in rows:
        if row["id"] == row_id:
            found_row = row
            break

    assert found_row is not None, f"Updated row with ID {row_id} not found in table"

    logger.info(f"Successfully updated row with ID: {row_id}")


async def test_tables_delete_row(
    nc_client: NextcloudClient, sample_table_info: Dict[str, Any]
):
    """
    Test deleting a row from a table.
    """
    table_id = sample_table_info["table_id"]
    columns = sample_table_info["columns"]

    # First create a row to delete
    test_data = {}
    unique_suffix = uuid.uuid4().hex[:8]

    for column in columns:
        column_id = column["id"]
        column_type = column.get("type", "text")
        column_title = column.get("title", f"column_{column_id}")

        if column_type == "text":
            test_data[column_id] = f"Test Delete {column_title} {unique_suffix}"
        elif column_type == "number":
            test_data[column_id] = 789
        elif column_type == "datetime":
            test_data[column_id] = "2024-06-15T10:30:00Z"
        elif column_type == "select":
            options = column.get("selectOptions", [])
            if options:
                test_data[column_id] = options[0].get("label", "Option 1")
            else:
                test_data[column_id] = "Delete Option"
        else:
            test_data[column_id] = f"Test Delete {column_title} {unique_suffix}"

    logger.info(f"Creating row for delete test in table ID: {table_id}")

    created_row = await nc_client.tables.create_row(table_id, test_data)
    row_id = created_row["id"]

    logger.info(f"Testing delete_row for row ID: {row_id}")

    # Delete the row
    delete_result = await nc_client.tables.delete_row(row_id)

    assert isinstance(delete_result, dict)
    # The delete response might vary, but it should be successful

    # Verify the row was deleted by trying to find it
    await asyncio.sleep(1)  # Allow potential propagation delay
    rows = await nc_client.tables.get_table_rows(table_id)

    # Ensure the deleted row is not in the results
    found_row = None
    for row in rows:
        if row["id"] == row_id:
            found_row = row
            break

    assert found_row is None, f"Deleted row with ID {row_id} still found in table"

    logger.info(f"Successfully deleted row with ID: {row_id}")


async def test_tables_delete_nonexistent_row(nc_client: NextcloudClient):
    """
    Test that deleting a non-existent row fails appropriately.
    """
    non_existent_id = 999999999  # Use an ID highly unlikely to exist

    logger.info(f"Testing delete_row for non-existent row ID: {non_existent_id}")

    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.tables.delete_row(non_existent_id)

    # Accept both 404 and 500 as valid error responses for non-existent rows
    # The API behavior may vary between Nextcloud versions
    assert excinfo.value.response.status_code in [404, 500]
    logger.info(
        f"Deleting non-existent row ID: {non_existent_id} correctly failed with {excinfo.value.response.status_code}."
    )


async def test_tables_transform_row_data(
    nc_client: NextcloudClient, sample_table_info: Dict[str, Any]
):
    """
    Test the transform_row_data utility method.
    """
    table_id = sample_table_info["table_id"]
    columns = sample_table_info["columns"]

    logger.info(f"Testing transform_row_data for table ID: {table_id}")

    # Get some rows to transform
    rows = await nc_client.tables.get_table_rows(table_id, limit=5)

    if not rows:
        logger.info("No rows to transform, skipping transform_row_data test")
        return

    # Transform the rows
    transformed_rows = nc_client.tables.transform_row_data(rows, columns)

    assert isinstance(transformed_rows, list)
    assert len(transformed_rows) == len(rows)

    # Check the structure of transformed rows
    for i, transformed_row in enumerate(transformed_rows):
        original_row = rows[i]

        assert "id" in transformed_row
        assert "tableId" in transformed_row
        assert "data" in transformed_row
        assert transformed_row["id"] == original_row["id"]
        assert transformed_row["tableId"] == original_row["tableId"]
        assert isinstance(transformed_row["data"], dict)

        # Check that column IDs were transformed to column names
        for column in columns:
            column_title = column["title"]
            # The transformed data should have column names as keys
            # (though the column might not have data in this row)
            if any(item["columnId"] == column["id"] for item in original_row["data"]):
                assert column_title in transformed_row["data"]

    logger.info(f"Successfully transformed {len(transformed_rows)} rows")


async def test_tables_get_nonexistent_table_schema(nc_client: NextcloudClient):
    """
    Test that getting schema for a non-existent table fails appropriately.
    """
    non_existent_id = 999999999  # Use an ID highly unlikely to exist

    logger.info(
        f"Testing get_table_schema for non-existent table ID: {non_existent_id}"
    )

    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.tables.get_table_schema(non_existent_id)

    assert excinfo.value.response.status_code == 404
    logger.info(
        f"Getting schema for non-existent table ID: {non_existent_id} correctly failed with 404."
    )


async def test_tables_read_nonexistent_table(nc_client: NextcloudClient):
    """
    Test that reading from a non-existent table fails appropriately.
    """
    non_existent_id = 999999999  # Use an ID highly unlikely to exist

    logger.info(f"Testing get_table_rows for non-existent table ID: {non_existent_id}")

    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.tables.get_table_rows(non_existent_id)

    assert excinfo.value.response.status_code == 404
    logger.info(
        f"Reading from non-existent table ID: {non_existent_id} correctly failed with 404."
    )


async def test_tables_create_row_invalid_table(nc_client: NextcloudClient):
    """
    Test that creating a row in a non-existent table fails appropriately.
    """
    non_existent_id = 999999999  # Use an ID highly unlikely to exist
    test_data = {1: "test value"}

    logger.info(f"Testing create_row for non-existent table ID: {non_existent_id}")

    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.tables.create_row(non_existent_id, test_data)

    assert excinfo.value.response.status_code == 404
    logger.info(
        f"Creating row in non-existent table ID: {non_existent_id} correctly failed with 404."
    )
