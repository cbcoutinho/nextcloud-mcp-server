"""Tests for OIDC resource server scope prefixing."""

import pytest

from nextcloud_mcp_server.auth.oauth_routes import _transform_scopes_for_idp

pytestmark = pytest.mark.unit


class TestTransformScopesForIdp:
    """Test _transform_scopes_for_idp scope transformation."""

    def test_no_prefix_when_resource_server_id_empty(self):
        """Scopes are returned unchanged when resource_server_id is empty."""
        scopes = "openid profile notes.read notes.write"
        assert _transform_scopes_for_idp(scopes, "") == scopes

    def test_oidc_scopes_not_prefixed(self):
        """Standard OIDC scopes are never prefixed."""
        result = _transform_scopes_for_idp(
            "openid profile email", "https://api.example.com"
        )
        assert result == "openid profile email"

    def test_offline_access_not_prefixed(self):
        """offline_access is a standard OIDC scope and must not be prefixed."""
        result = _transform_scopes_for_idp(
            "openid offline_access notes.read", "https://api.example.com"
        )
        assert result == "openid offline_access https://api.example.com/notes.read"

    def test_resource_scopes_prefixed(self):
        """Non-OIDC scopes are prefixed with the resource server identifier."""
        result = _transform_scopes_for_idp(
            "notes.read notes.write", "https://api.example.com"
        )
        assert (
            result
            == "https://api.example.com/notes.read https://api.example.com/notes.write"
        )

    def test_mixed_scopes(self):
        """Mixed OIDC and resource scopes are handled correctly."""
        result = _transform_scopes_for_idp(
            "openid profile notes.read calendar.write offline_access",
            "https://api.example.com",
        )
        assert result == (
            "openid profile https://api.example.com/notes.read "
            "https://api.example.com/calendar.write offline_access"
        )

    @pytest.mark.parametrize(
        ("resource_server_id", "expected_prefix"),
        [
            ("https://api.example.com", "https://api.example.com/notes.read"),
            ("my-api", "my-api/notes.read"),
            ("urn:api:prod", "urn:api:prod/notes.read"),
        ],
    )
    def test_various_identifier_formats(self, resource_server_id, expected_prefix):
        """Different resource server identifier formats are supported."""
        result = _transform_scopes_for_idp("notes.read", resource_server_id)
        assert result == expected_prefix

    def test_single_resource_scope(self):
        """A single non-OIDC scope is prefixed."""
        result = _transform_scopes_for_idp("notes.read", "https://api.example.com")
        assert result == "https://api.example.com/notes.read"

    def test_empty_scopes_string(self):
        """An empty scopes string returns empty."""
        result = _transform_scopes_for_idp("", "https://api.example.com")
        assert result == ""

    def test_already_prefixed_scopes_not_double_prefixed(self):
        """Scopes already carrying the resource server prefix are not prefixed again."""
        result = _transform_scopes_for_idp(
            "https://api.example.com/notes.read notes.write",
            "https://api.example.com",
        )
        assert result == (
            "https://api.example.com/notes.read https://api.example.com/notes.write"
        )
