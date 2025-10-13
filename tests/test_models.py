"""Unit tests for Pydantic models and serialization."""

import json
import logging
import re
from datetime import datetime, timezone

from nextcloud_mcp_server.models.base import BaseResponse

logger = logging.getLogger(__name__)


def test_timestamp_format_validation():
    """Test that timestamps in BaseResponse are RFC3339 compliant for MCP validation.

    This test should initially fail, demonstrating the timestamp validation error
    seen in MCP inspector. MCP expects RFC3339 format with timezone information.
    """
    # Create a response object
    response = BaseResponse()

    # Serialize to JSON (mimics what MCP inspector sees)
    json_str = response.model_dump_json()
    data = json.loads(json_str)

    timestamp_str = data["timestamp"]

    # RFC3339 regex pattern (what MCP expects)
    # Format: YYYY-MM-DDTHH:MM:SS[.ffffff][Z|±HH:MM]
    rfc3339_pattern = (
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
    )

    # This assertion should FAIL with current implementation
    assert re.match(rfc3339_pattern, timestamp_str), (
        f"Timestamp '{timestamp_str}' is not RFC3339 compliant. "
        f"MCP expects format like '2025-08-30T19:22:58.862377Z' or '2025-08-30T19:22:58.862377+00:00'"
    )


def test_base_response_timestamp_is_utc():
    """Test that BaseResponse timestamps are in UTC timezone."""
    response = BaseResponse()

    # The timestamp should be timezone-aware and in UTC
    assert response.timestamp.tzinfo is not None, (
        "Timestamp should have timezone information"
    )
    assert response.timestamp.tzinfo == timezone.utc, (
        "Timestamp should be in UTC timezone"
    )


def test_serialized_timestamp_ends_with_z_or_offset():
    """Test that serialized timestamps have proper timezone suffix."""
    response = BaseResponse()
    json_str = response.model_dump_json()
    data = json.loads(json_str)

    timestamp_str = data["timestamp"]

    # Should end with 'Z' (UTC) or timezone offset like '+00:00'
    assert timestamp_str.endswith("Z") or re.search(
        r"[+-]\d{2}:\d{2}$", timestamp_str
    ), (
        f"Timestamp '{timestamp_str}' should end with 'Z' or timezone offset like '+00:00'"
    )


def test_current_broken_format():
    """Test showing the current broken timestamp format that causes MCP validation errors."""
    # This demonstrates what the current code produces
    current_naive_dt = datetime.now()
    current_format = current_naive_dt.isoformat()

    # Show that current format lacks timezone info
    assert "Z" not in current_format
    assert "+" not in current_format
    assert "-" not in current_format[-6:]  # Check last 6 chars for timezone

    logger.info(f"Current broken format: {current_format}")
    logger.info(
        "This format causes MCP validation errors because it lacks timezone information"
    )
