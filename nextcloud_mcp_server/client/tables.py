"""Client for Nextcloud Tables app operations."""

import logging
from typing import Any, Dict, List, Optional

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


class TablesClient(BaseNextcloudClient):
    """Client for Nextcloud Tables app operations."""

    app_name = "tables"

    async def list_tables(self) -> List[Dict[str, Any]]:
        """List all tables available to the user."""
        response = await self._make_request(
            "GET",
            "/ocs/v2.php/apps/tables/api/2/tables",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        result = response.json()
        return result["ocs"]["data"]

    async def get_table_schema(self, table_id: int) -> Dict[str, Any]:
        """Get the schema/structure of a specific table including columns and views."""
        # Using v1 API as v2 schema endpoint had issues during testing
        response = await self._make_request(
            "GET", f"/index.php/apps/tables/api/1/tables/{table_id}/scheme"
        )
        return response.json()

    async def get_table_rows(
        self, table_id: int, limit: Optional[int] = None, offset: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Read rows from a table with optional pagination."""
        params = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        response = await self._make_request(
            "GET", f"/index.php/apps/tables/api/1/tables/{table_id}/rows", params=params
        )
        return response.json()

    async def create_row(self, table_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a new row into a table.

        Args:
            table_id: ID of the table to insert into
            data: Dictionary mapping column IDs to values, e.g. {1: "text", 2: 42}
        """
        # Transform data to API format: {"data": {"1": "text", "2": 42}}
        api_data = {str(k): v for k, v in data.items()}

        response = await self._make_request(
            "POST",
            f"/ocs/v2.php/apps/tables/api/2/tables/{table_id}/rows",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            json={"data": api_data},
        )
        result = response.json()
        return result["ocs"]["data"]

    async def update_row(self, row_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update an existing row in a table.

        Args:
            row_id: ID of the row to update
            data: Dictionary mapping column IDs to new values, e.g. {1: "new text", 2: 99}
        """
        # Transform data to API format for v1 endpoint
        api_data = {str(k): v for k, v in data.items()}

        response = await self._make_request(
            "PUT",
            f"/index.php/apps/tables/api/1/rows/{row_id}",
            json={"data": api_data},
        )
        return response.json()

    async def delete_row(self, row_id: int) -> Dict[str, Any]:
        """Delete a row from a table."""
        response = await self._make_request(
            "DELETE", f"/index.php/apps/tables/api/1/rows/{row_id}"
        )
        return response.json()

    def transform_row_data(
        self, rows: List[Dict[str, Any]], columns: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Transform raw row data into more readable format using column names.

        Args:
            rows: Raw row data from the API
            columns: Column definitions from table schema

        Returns:
            List of rows with column names as keys instead of column IDs
        """
        # Create mapping from column ID to column title
        column_map = {col["id"]: col["title"] for col in columns}

        transformed_rows = []
        for row in rows:
            transformed_row = {
                "id": row["id"],
                "tableId": row["tableId"],
                "createdBy": row["createdBy"],
                "createdAt": row["createdAt"],
                "lastEditBy": row["lastEditBy"],
                "lastEditAt": row["lastEditAt"],
                "data": {},
            }

            # Transform data array to column_name: value mapping
            for item in row["data"]:
                column_id = item["columnId"]
                column_name = column_map.get(column_id, f"column_{column_id}")
                transformed_row["data"][column_name] = item["value"]

            transformed_rows.append(transformed_row)

        return transformed_rows
