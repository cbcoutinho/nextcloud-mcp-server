"""
Unit tests for Management API app password endpoints.

Tests the REST API endpoints for multi-user BasicAuth mode app password management:
- POST /api/v1/users/{user_id}/app-password - Provision app password
- GET /api/v1/users/{user_id}/app-password - Check status
- DELETE /api/v1/users/{user_id}/app-password - Delete app password
"""

import base64
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api import management
from nextcloud_mcp_server.api.management import (
    delete_app_password,
    get_app_password_status,
    provision_app_password,
)
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_rate_limit():
    """Clear rate limit state before each test."""
    management._rate_limit_attempts.clear()
    yield
    management._rate_limit_attempts.clear()


@pytest.fixture
def encryption_key():
    """Generate a test encryption key."""
    return Fernet.generate_key().decode()


@pytest.fixture
async def temp_storage(encryption_key):
    """Create temporary storage instance with encryption for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_management.db"
        storage = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=encryption_key
        )
        await storage.initialize()
        yield storage


def create_basic_auth_header(username: str, password: str) -> str:
    """Create BasicAuth header value."""
    credentials = f"{username}:{password}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


def create_test_app(storage):
    """Create a test Starlette app with the management endpoints."""
    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            ),
            Route(
                "/api/v1/users/{user_id}/app-password",
                get_app_password_status,
                methods=["GET"],
            ),
            Route(
                "/api/v1/users/{user_id}/app-password",
                delete_app_password,
                methods=["DELETE"],
            ),
        ]
    )
    app.state.storage = storage
    return app


async def test_provision_app_password_missing_auth():
    """Test that missing auth returns 401."""
    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            ),
        ]
    )

    client = TestClient(app)
    response = client.post("/api/v1/users/testuser/app-password")

    assert response.status_code == 401
    assert "Missing BasicAuth" in response.json()["error"]


async def test_provision_app_password_invalid_auth_format():
    """Test that invalid auth format returns 401."""
    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            ),
        ]
    )

    client = TestClient(app)
    response = client.post(
        "/api/v1/users/testuser/app-password",
        headers={"Authorization": "Basic invalid-not-base64!!!"},
    )

    assert response.status_code == 401
    assert "Invalid BasicAuth" in response.json()["error"]


async def test_provision_app_password_username_mismatch():
    """Test that username mismatch returns 403."""
    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            ),
        ]
    )

    client = TestClient(app)
    # Try to provision for "testuser" but auth as "otheruser"
    response = client.post(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "otheruser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 403
    assert "does not match" in response.json()["error"]


async def test_provision_app_password_invalid_format():
    """Test that invalid app password format returns 400."""
    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            ),
        ]
    )

    client = TestClient(app)
    # Use invalid password format (not xxxxx-xxxxx-xxxxx-xxxxx-xxxxx)
    response = client.post(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header("testuser", "invalid-password")
        },
    )

    assert response.status_code == 400
    assert "Invalid app password format" in response.json()["error"]


async def test_provision_app_password_success(temp_storage, mocker):
    """Test successful app password provisioning."""
    # Mock settings (imported locally in the function)
    mocker.patch(
        "nextcloud_mcp_server.config.get_settings",
        return_value=MagicMock(nextcloud_host="http://localhost:8080"),
    )

    # Mock httpx client for Nextcloud validation
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"ocs": {"data": {"id": "testuser"}}}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    mocker.patch(
        "nextcloud_mcp_server.api.management.httpx.AsyncClient",
        return_value=mock_client,
    )

    # Create app with storage
    app = create_test_app(temp_storage)

    client = TestClient(app)
    response = client.post(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "stored" in data["message"].lower()

    # Verify password was stored
    stored_password = await temp_storage.get_app_password("testuser")
    assert stored_password == "aaaaa-bbbbb-ccccc-ddddd-eeeee"


async def test_provision_app_password_nextcloud_validation_fails(mocker):
    """Test that failed Nextcloud validation returns 401."""
    mocker.patch(
        "nextcloud_mcp_server.config.get_settings",
        return_value=MagicMock(nextcloud_host="http://localhost:8080"),
    )

    # Mock httpx client to return 401
    mock_response = MagicMock()
    mock_response.status_code = 401

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    mocker.patch(
        "nextcloud_mcp_server.api.management.httpx.AsyncClient",
        return_value=mock_client,
    )

    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            ),
        ]
    )

    client = TestClient(app)
    response = client.post(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 401
    assert "Invalid app password" in response.json()["error"]


async def test_get_app_password_status_provisioned(temp_storage, mocker):
    """Test checking status when app password is provisioned."""
    # Store an app password
    await temp_storage.store_app_password("testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee")

    app = create_test_app(temp_storage)

    client = TestClient(app)
    response = client.get(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["user_id"] == "testuser"
    assert data["has_app_password"] is True


async def test_get_app_password_status_not_provisioned(temp_storage, mocker):
    """Test checking status when app password is not provisioned."""
    app = create_test_app(temp_storage)

    client = TestClient(app)
    response = client.get(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["user_id"] == "testuser"
    assert data["has_app_password"] is False


async def test_get_app_password_status_username_mismatch():
    """Test that username mismatch returns 403 for status check."""
    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                get_app_password_status,
                methods=["GET"],
            ),
        ]
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "otheruser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 403


async def test_delete_app_password_success(temp_storage, mocker):
    """Test successful app password deletion."""
    # Store an app password
    await temp_storage.store_app_password("testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee")

    # Mock settings (imported locally in the function)
    mocker.patch(
        "nextcloud_mcp_server.config.get_settings",
        return_value=MagicMock(nextcloud_host="http://localhost:8080"),
    )

    # Mock httpx client for Nextcloud validation
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    mocker.patch(
        "nextcloud_mcp_server.api.management.httpx.AsyncClient",
        return_value=mock_client,
    )

    app = create_test_app(temp_storage)

    client = TestClient(app)
    response = client.delete(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "deleted" in data["message"].lower()

    # Verify password was removed
    stored_password = await temp_storage.get_app_password("testuser")
    assert stored_password is None


async def test_delete_app_password_not_found(temp_storage, mocker):
    """Test deleting non-existent app password."""
    # Mock settings (imported locally in the function)
    mocker.patch(
        "nextcloud_mcp_server.config.get_settings",
        return_value=MagicMock(nextcloud_host="http://localhost:8080"),
    )

    # Mock httpx client for Nextcloud validation
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    mocker.patch(
        "nextcloud_mcp_server.api.management.httpx.AsyncClient",
        return_value=mock_client,
    )

    app = create_test_app(temp_storage)

    client = TestClient(app)
    response = client.delete(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "no app password found" in data["message"].lower()


async def test_delete_app_password_invalid_credentials(mocker):
    """Test that invalid credentials returns 401 for deletion."""
    mocker.patch(
        "nextcloud_mcp_server.config.get_settings",
        return_value=MagicMock(nextcloud_host="http://localhost:8080"),
    )

    # Mock httpx client to return 401
    mock_response = MagicMock()
    mock_response.status_code = 401

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    mocker.patch(
        "nextcloud_mcp_server.api.management.httpx.AsyncClient",
        return_value=mock_client,
    )

    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                delete_app_password,
                methods=["DELETE"],
            ),
        ]
    )

    client = TestClient(app)
    response = client.delete(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "testuser", "wrong-password-xxxxx"
            )
        },
    )

    assert response.status_code == 401
    assert "Invalid credentials" in response.json()["error"]


async def test_delete_app_password_username_mismatch():
    """Test that username mismatch returns 403 for deletion."""
    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                delete_app_password,
                methods=["DELETE"],
            ),
        ]
    )

    client = TestClient(app)
    response = client.delete(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "otheruser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )

    assert response.status_code == 403


async def test_provision_app_password_rate_limiting(mocker):
    """Test that rate limiting blocks excessive provisioning attempts."""
    mocker.patch(
        "nextcloud_mcp_server.config.get_settings",
        return_value=MagicMock(nextcloud_host="http://localhost:8080"),
    )

    # Mock httpx client to return 401 (failed validation)
    mock_response = MagicMock()
    mock_response.status_code = 401

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    mocker.patch(
        "nextcloud_mcp_server.api.management.httpx.AsyncClient",
        return_value=mock_client,
    )

    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            ),
        ]
    )

    client = TestClient(app)

    # Make 5 failed attempts (should all return 401)
    for i in range(5):
        response = client.post(
            "/api/v1/users/testuser/app-password",
            headers={
                "Authorization": create_basic_auth_header(
                    "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
                )
            },
        )
        assert response.status_code == 401, f"Attempt {i + 1} should return 401"

    # 6th attempt should be rate limited (429)
    response = client.post(
        "/api/v1/users/testuser/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "testuser", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )
    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json()["error"]
    assert "Retry-After" in response.headers


async def test_rate_limiting_is_per_user(mocker):
    """Test that rate limiting is applied per user, not globally."""
    mocker.patch(
        "nextcloud_mcp_server.config.get_settings",
        return_value=MagicMock(nextcloud_host="http://localhost:8080"),
    )

    # Mock httpx client to return 401
    mock_response = MagicMock()
    mock_response.status_code = 401

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    mocker.patch(
        "nextcloud_mcp_server.api.management.httpx.AsyncClient",
        return_value=mock_client,
    )

    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/app-password",
                provision_app_password,
                methods=["POST"],
            ),
        ]
    )

    client = TestClient(app)

    # Make 5 failed attempts for user1 (hits rate limit)
    for _ in range(5):
        client.post(
            "/api/v1/users/user1/app-password",
            headers={
                "Authorization": create_basic_auth_header(
                    "user1", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
                )
            },
        )

    # user1 should be rate limited
    response = client.post(
        "/api/v1/users/user1/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "user1", "aaaaa-bbbbb-ccccc-ddddd-eeeee"
            )
        },
    )
    assert response.status_code == 429

    # user2 should NOT be rate limited (different user)
    response = client.post(
        "/api/v1/users/user2/app-password",
        headers={
            "Authorization": create_basic_auth_header(
                "user2", "bbbbb-ccccc-ddddd-eeeee-fffff"
            )
        },
    )
    assert response.status_code == 401  # Fails validation, but not rate limited
