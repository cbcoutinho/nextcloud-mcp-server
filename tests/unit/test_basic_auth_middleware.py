"""Unit tests for BasicAuthMiddleware."""

import base64

import pytest

from nextcloud_mcp_server.app import BasicAuthMiddleware


class MockApp:
    """Mock ASGI app for testing middleware."""

    def __init__(self):
        self.called = False
        self.received_scope = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.received_scope = scope


@pytest.mark.unit
async def test_basic_auth_middleware_valid_credentials():
    """Test that middleware correctly extracts valid BasicAuth credentials."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    credentials = base64.b64encode(b"admin:password123").decode("utf-8")
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Basic {credentials}".encode())],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    assert "state" in scope
    assert "basic_auth" in scope["state"]
    assert scope["state"]["basic_auth"]["username"] == "admin"
    assert scope["state"]["basic_auth"]["password"] == "password123"


@pytest.mark.unit
async def test_basic_auth_middleware_password_with_colon():
    """Test that middleware handles passwords containing colons."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    # Password contains colon - should split on first colon only
    credentials = base64.b64encode(b"user:pass:word:123").decode("utf-8")
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Basic {credentials}".encode())],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert scope["state"]["basic_auth"]["username"] == "user"
    assert scope["state"]["basic_auth"]["password"] == "pass:word:123"


@pytest.mark.unit
async def test_basic_auth_middleware_invalid_base64():
    """Test that middleware handles invalid base64 encoding gracefully."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Basic INVALID_BASE64!!!")],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    # Should not have basic_auth in state due to error
    assert "basic_auth" not in scope.get("state", {})


@pytest.mark.unit
async def test_basic_auth_middleware_missing_authorization_header():
    """Test that middleware handles missing Authorization header."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    scope = {
        "type": "http",
        "headers": [],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    # Should not have basic_auth in state
    assert "basic_auth" not in scope.get("state", {})


@pytest.mark.unit
async def test_basic_auth_middleware_wrong_auth_scheme():
    """Test that middleware ignores non-Basic auth schemes."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer some_token")],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    # Should not have basic_auth in state
    assert "basic_auth" not in scope.get("state", {})


@pytest.mark.unit
async def test_basic_auth_middleware_malformed_credentials():
    """Test that middleware handles credentials without colon separator."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    # Credentials without colon separator
    credentials = base64.b64encode(b"username_no_password").decode("utf-8")
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Basic {credentials}".encode())],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    # Should not have basic_auth in state due to error
    assert "basic_auth" not in scope.get("state", {})


@pytest.mark.unit
async def test_basic_auth_middleware_non_http_scope():
    """Test that middleware passes through non-HTTP scopes unchanged."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    scope = {
        "type": "websocket",
        "headers": [(b"authorization", b"Basic dXNlcjpwYXNz")],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    # Should not process websocket scopes
    assert "state" not in scope


@pytest.mark.unit
async def test_basic_auth_middleware_preserves_existing_state():
    """Test that middleware preserves existing state data."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    credentials = base64.b64encode(b"user:pass").decode("utf-8")
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Basic {credentials}".encode())],
        "state": {"existing_key": "existing_value"},
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    assert scope["state"]["existing_key"] == "existing_value"
    assert scope["state"]["basic_auth"]["username"] == "user"
    assert scope["state"]["basic_auth"]["password"] == "pass"


@pytest.mark.unit
async def test_basic_auth_middleware_empty_password():
    """Test that middleware handles empty passwords."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    credentials = base64.b64encode(b"user:").decode("utf-8")
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Basic {credentials}".encode())],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    assert scope["state"]["basic_auth"]["username"] == "user"
    assert scope["state"]["basic_auth"]["password"] == ""


@pytest.mark.unit
async def test_basic_auth_middleware_unicode_credentials():
    """Test that middleware handles Unicode characters in credentials."""
    # Arrange
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    # Username and password with Unicode characters
    credentials = base64.b64encode("üser:pässwörd".encode("utf-8")).decode("utf-8")
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Basic {credentials}".encode())],
    }

    # Act
    await middleware(scope, None, None)  # type: ignore[arg-type]

    # Assert
    assert mock_app.called
    assert scope["state"]["basic_auth"]["username"] == "üser"
    assert scope["state"]["basic_auth"]["password"] == "pässwörd"


@pytest.mark.unit
async def test_basic_auth_middleware_stores_bearer_token():
    """A Bearer header is stored for pass-through, not parsed as BasicAuth."""
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer eyJhbGciOi.payload.sig")],
    }

    await middleware(scope, None, None)  # type: ignore[arg-type]

    assert mock_app.called
    assert scope["state"]["bearer_token"] == "eyJhbGciOi.payload.sig"
    assert "basic_auth" not in scope["state"]


@pytest.mark.unit
async def test_basic_auth_middleware_bearer_scheme_case_insensitive():
    """The auth scheme is case-insensitive (RFC 9110 11.1)."""
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    scope = {
        "type": "http",
        "headers": [(b"authorization", b"bearer abc123")],
    }

    await middleware(scope, None, None)  # type: ignore[arg-type]

    assert scope["state"]["bearer_token"] == "abc123"


@pytest.mark.unit
async def test_basic_auth_middleware_empty_bearer_ignored():
    """An empty bearer token is not stored."""
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer   ")],
    }

    await middleware(scope, None, None)  # type: ignore[arg-type]

    assert mock_app.called
    assert "bearer_token" not in scope.get("state", {})


@pytest.mark.unit
async def test_basic_auth_middleware_basic_does_not_set_bearer():
    """BasicAuth requests are unchanged: no bearer_token in state."""
    mock_app = MockApp()
    middleware = BasicAuthMiddleware(mock_app)

    credentials = base64.b64encode(b"admin:password123").decode("utf-8")
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Basic {credentials}".encode())],
    }

    await middleware(scope, None, None)  # type: ignore[arg-type]

    assert scope["state"]["basic_auth"]["username"] == "admin"
    assert "bearer_token" not in scope["state"]
