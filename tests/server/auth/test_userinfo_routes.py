"""Unit tests for user info routes.

These unit tests cover the simple _query_idp_userinfo helper function.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from nextcloud_mcp_server.auth.userinfo_routes import (
    _jinja_env,
    _query_idp_userinfo,
    user_info_html,
)

pytestmark = pytest.mark.unit


async def test_query_idp_userinfo_success(mocker):
    """Test successful IdP userinfo query."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "sub": "alice",
        "email": "alice@example.com",
        "name": "Alice Smith",
    }
    mock_response.raise_for_status = Mock()

    # Mock the async context manager properly
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    mocker.patch(
        "nextcloud_mcp_server.auth.userinfo_routes.nextcloud_httpx_client",
        return_value=mock_client,
    )

    result = await _query_idp_userinfo("test_token", "https://example.com/userinfo")

    assert result == {
        "sub": "alice",
        "email": "alice@example.com",
        "name": "Alice Smith",
    }
    mock_client.get.assert_called_once_with(
        "https://example.com/userinfo",
        headers={"Authorization": "Bearer test_token"},
    )


async def test_query_idp_userinfo_failure(mocker):
    """Test IdP userinfo query failure handling."""
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("Network error")
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    mocker.patch(
        "nextcloud_mcp_server.auth.userinfo_routes.nextcloud_httpx_client",
        return_value=mock_client,
    )

    result = await _query_idp_userinfo("test_token", "https://example.com/userinfo")

    assert result is None


def test_templates_autoescape():
    """The Jinja environment must autoescape.

    Jinja does not do this by default, and without it `{{ error_message }}` --
    built from exception text -- renders raw into an authenticated page.
    """
    rendered = _jinja_env.get_template("error.html").render(
        error_message="<script>alert(1)</script>",
        error_title="<img src=x onerror=alert(1)>",
    )

    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "onerror=alert(1)>" not in rendered


def test_user_info_safe_fragments_are_still_raw():
    """The three `|safe` fragments must keep passing HTML through.

    Autoescaping them would double-escape markup this module builds itself; the
    values *inside* those fragments are escaped where they are read instead.
    """
    rendered = _jinja_env.get_template("user_info.html").render(
        user_info_tab_html="<table><tr><td>marker-cell</td></tr></table>",
        vector_sync_tab_html="",
        webhooks_tab_html="",
        show_vector_sync_tab=False,
        show_webhooks_tab=False,
        logout_url=None,
    )

    assert "<td>marker-cell</td>" in rendered


async def test_user_info_html_escapes_username_exactly_once(mocker):
    """A username with HTML-special characters is escaped once, not twice.

    `username` feeds two different sinks: the hand-built `|safe` fragment (which
    autoescape cannot reach, so it needs a pre-escaped copy) and the plain
    `{{ username }}` in the navbar (which autoescape handles itself). Passing
    the pre-escaped copy to both renders `O'Brien & Co` as
    `O&amp;#x27;Brien &amp;amp; Co`. This drives the real handler rather than
    the template, because rendering the template directly is exactly what missed
    the bug.
    """
    mocker.patch(
        "nextcloud_mcp_server.auth.userinfo_routes._get_user_info",
        return_value={"auth_mode": "basic", "username": "O'Brien & <Co>"},
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.userinfo_routes._get_processing_status",
        return_value=None,
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.userinfo_routes._get_authenticated_client_for_userinfo",
        side_effect=RuntimeError("not needed"),
    )

    request = Mock()
    request.app.state = Mock(spec=[])  # no oauth_context -> BasicAuth rendering

    # __wrapped__ skips the @requires('authenticated') gate, which asserts a
    # real starlette Request; the subject here is the escaping, not the gate.
    response = await user_info_html.__wrapped__(request)
    body = response.body.decode()

    # Escaped exactly once: the raw text and the double-escaped form are absent.
    assert "O&#x27;Brien &amp; &lt;Co&gt;" in body
    assert "&amp;#x27;" not in body
    assert "&amp;amp;" not in body
    assert "<Co>" not in body
