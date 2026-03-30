"""Unit tests for web-based Login Flow v2 provisioning routes.

Tests validation, HTML escaping, URL rewriting, route handlers, and
background polling logic.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextcloud_mcp_server.auth.login_flow import LoginFlowPollResult
from nextcloud_mcp_server.auth.provision_routes import (
    _poll_and_store,
    _provision_sessions,
    _render_error,
    _validate_redirect_uri,
    provision_page,
    provision_status,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_provision_sessions():
    """Ensure _provision_sessions is empty before and after each test."""
    _provision_sessions.clear()
    yield
    _provision_sessions.clear()


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
    request = _make_request({"id": provision_id})
    response = await provision_status(request)
    assert response.status_code == 200


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


async def test_provision_status_expired_by_ttl():
    """Session past its TTL is reported as expired and cleaned up."""
    provision_id = "test-expired-ttl"
    _provision_sessions[provision_id] = {
        "status": "pending",
        "expires_at": time.time() - 1,  # Already expired
    }
    request = _make_request({"id": provision_id})
    response = await provision_status(request)
    assert response.status_code == 404
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
        new_callable=AsyncMock,
        return_value=mock_storage,
    ):
        response = await provision_page(request)

    assert response.status_code == 307  # RedirectResponse default
    assert response.headers["location"] == "https://app.example.com/settings"


# ── _poll_and_store tests ────────────────────────────────────────────────


def _create_poll_session(provision_id: str) -> dict:
    """Create a minimal provision session for polling tests."""
    session = {
        "status": "pending",
        "poll_endpoint": "https://cloud.example.com/login/v2/poll",
        "poll_token": "secret-token",
        "user_id": "alice",
        "created_at": time.time(),
        "expires_at": time.time() + 1200,
    }
    _provision_sessions[provision_id] = session
    return session


async def test_poll_and_store_completed():
    """Successful poll stores app password and sets status to completed."""
    provision_id = "test-poll-completed"
    _create_poll_session(provision_id)

    mock_poll_result = LoginFlowPollResult(
        status="completed",
        server="https://cloud.example.com",
        login_name="alice",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
    )

    mock_flow_client = AsyncMock()
    mock_flow_client.poll.return_value = mock_poll_result

    mock_storage = AsyncMock()

    mock_settings = MagicMock()
    mock_settings.nextcloud_host = "https://cloud.example.com"

    with (
        patch(
            "nextcloud_mcp_server.auth.provision_routes.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.provision_routes.get_nextcloud_ssl_verify",
            return_value=False,
        ),
        patch(
            "nextcloud_mcp_server.auth.provision_routes.LoginFlowV2Client",
            return_value=mock_flow_client,
        ),
        patch(
            "nextcloud_mcp_server.auth.provision_routes.get_shared_storage",
            new_callable=AsyncMock,
            return_value=mock_storage,
        ),
    ):
        await _poll_and_store(provision_id)

    assert _provision_sessions[provision_id]["status"] == "completed"
    assert _provision_sessions[provision_id]["username"] == "alice"
    mock_storage.store_app_password_with_scopes.assert_called_once_with(
        user_id="alice",
        app_password="aaaaa-bbbbb-ccccc-ddddd-eeeee",
        scopes=None,
        username="alice",
    )


async def test_poll_and_store_expired():
    """Expired poll result sets session status to expired."""
    provision_id = "test-poll-expired"
    _create_poll_session(provision_id)

    mock_poll_result = LoginFlowPollResult(status="expired")

    mock_flow_client = AsyncMock()
    mock_flow_client.poll.return_value = mock_poll_result

    mock_settings = MagicMock()
    mock_settings.nextcloud_host = "https://cloud.example.com"

    with (
        patch(
            "nextcloud_mcp_server.auth.provision_routes.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.provision_routes.get_nextcloud_ssl_verify",
            return_value=False,
        ),
        patch(
            "nextcloud_mcp_server.auth.provision_routes.LoginFlowV2Client",
            return_value=mock_flow_client,
        ),
    ):
        await _poll_and_store(provision_id)

    assert _provision_sessions[provision_id]["status"] == "expired"


async def test_poll_and_store_missing_app_password():
    """Completed poll with no app_password sets status to error."""
    provision_id = "test-poll-no-password"
    _create_poll_session(provision_id)

    mock_poll_result = LoginFlowPollResult(
        status="completed",
        server="https://cloud.example.com",
        login_name="alice",
        app_password=None,  # Missing
    )

    mock_flow_client = AsyncMock()
    mock_flow_client.poll.return_value = mock_poll_result

    mock_settings = MagicMock()
    mock_settings.nextcloud_host = "https://cloud.example.com"

    mock_storage = AsyncMock()

    with (
        patch(
            "nextcloud_mcp_server.auth.provision_routes.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.provision_routes.get_nextcloud_ssl_verify",
            return_value=False,
        ),
        patch(
            "nextcloud_mcp_server.auth.provision_routes.LoginFlowV2Client",
            return_value=mock_flow_client,
        ),
        patch(
            "nextcloud_mcp_server.auth.provision_routes.get_shared_storage",
            new_callable=AsyncMock,
            return_value=mock_storage,
        ),
    ):
        await _poll_and_store(provision_id)

    assert _provision_sessions[provision_id]["status"] == "error"
    mock_storage.store_app_password_with_scopes.assert_not_called()


async def test_poll_and_store_session_cleaned_up():
    """Poll exits early if session was cleaned up externally."""
    provision_id = "test-poll-cleaned"
    # Don't create session — simulate it being cleaned up before poll starts

    mock_settings = MagicMock()
    mock_settings.nextcloud_host = "https://cloud.example.com"

    with patch(
        "nextcloud_mcp_server.auth.provision_routes.get_settings",
        return_value=mock_settings,
    ):
        # Should return immediately without error
        await _poll_and_store(provision_id)

    assert provision_id not in _provision_sessions
