"""Unit tests for web-based Login Flow v2 provisioning routes.

Tests validation, HTML escaping, URL rewriting, and route handlers.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextcloud_mcp_server.auth.provision_routes import (
    _provision_sessions,
    _render_error,
    _validate_redirect_uri,
    provision_page,
    provision_status,
)

pytestmark = pytest.mark.unit


# ── _validate_redirect_uri tests ─────────────────────────────────────────


async def test_validate_redirect_uri_accepts_https():
    """Valid HTTPS URL is accepted."""
    assert _validate_redirect_uri("https://app.example.com/callback") is True


async def test_validate_redirect_uri_accepts_http_localhost():
    """Valid HTTP localhost URL is accepted."""
    assert _validate_redirect_uri("http://localhost:3000/callback") is True


async def test_validate_redirect_uri_rejects_javascript():
    """javascript: URIs are rejected."""
    assert _validate_redirect_uri("javascript:alert(1)") is False


async def test_validate_redirect_uri_rejects_relative_url():
    """Relative URLs are rejected (no scheme/netloc)."""
    assert _validate_redirect_uri("/relative/path") is False


async def test_validate_redirect_uri_rejects_bare_hostname():
    """Bare hostnames without scheme are rejected."""
    assert _validate_redirect_uri("example.com") is False


async def test_validate_redirect_uri_rejects_data_uri():
    """data: URIs are rejected."""
    assert _validate_redirect_uri("data:text/html,<h1>hi</h1>") is False


async def test_validate_redirect_uri_rejects_empty():
    """Empty string is rejected."""
    assert _validate_redirect_uri("") is False


# ── _render_error tests ──────────────────────────────────────────────────


async def test_render_error_escapes_html():
    """XSS regression: HTML in error messages must be escaped."""
    html_output = _render_error("<script>alert('xss')</script>")
    assert "<script>" not in html_output
    assert "&lt;script&gt;" in html_output


async def test_render_error_escapes_angle_brackets():
    """Angle brackets in exception messages are escaped."""
    html_output = _render_error("Unexpected response from <internal-host>")
    assert "<internal-host>" not in html_output
    assert "&lt;internal-host&gt;" in html_output


async def test_render_error_preserves_plain_text():
    """Plain text messages render correctly."""
    html_output = _render_error("Something went wrong.")
    assert "Something went wrong." in html_output
    assert "Provisioning Error" in html_output


# ── provision_status tests ───────────────────────────────────────────────


def _make_request(query_params: dict) -> MagicMock:
    """Create a mock Starlette Request with query_params."""
    request = MagicMock()
    request.query_params = query_params
    return request


async def test_provision_status_not_found():
    """Unknown provision ID returns 404."""
    request = _make_request({"id": "nonexistent-id"})
    response = await provision_status(request)
    assert response.status_code == 404
    assert response.body is not None


async def test_provision_status_pending():
    """Pending session returns status=pending."""
    provision_id = "test-pending-id"
    _provision_sessions[provision_id] = {
        "status": "pending",
        "expires_at": time.time() + 600,
    }
    try:
        request = _make_request({"id": provision_id})
        response = await provision_status(request)
        assert response.status_code == 200
    finally:
        _provision_sessions.pop(provision_id, None)


async def test_provision_status_completed_cleans_up():
    """Completed session returns username and removes session."""
    provision_id = "test-completed-id"
    _provision_sessions[provision_id] = {
        "status": "completed",
        "username": "alice",
        "expires_at": time.time() + 600,
    }
    request = _make_request({"id": provision_id})
    response = await provision_status(request)
    assert response.status_code == 200
    # Session should be cleaned up after status read
    assert provision_id not in _provision_sessions


# ── provision_page tests ─────────────────────────────────────────────────


async def test_provision_page_missing_redirect_uri():
    """Missing redirect_uri returns 400."""
    request = _make_request({})
    response = await provision_page(request)
    assert response.status_code == 400


async def test_provision_page_invalid_redirect_uri():
    """Invalid redirect_uri (javascript:) returns 400."""
    request = _make_request({"redirect_uri": "javascript:alert(1)"})
    response = await provision_page(request)
    assert response.status_code == 400


async def test_provision_page_skips_if_already_provisioned():
    """If user already has an app password, redirect immediately."""
    request = _make_request(
        {
            "redirect_uri": "https://app.example.com/settings",
            "user_id": "alice",
        }
    )

    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = {
        "app_password": "existing-password",
    }

    with patch(
        "nextcloud_mcp_server.auth.provision_routes.get_shared_storage",
        return_value=mock_storage,
    ):
        response = await provision_page(request)

    assert response.status_code == 307  # RedirectResponse default
    assert response.headers["location"] == "https://app.example.com/settings"
