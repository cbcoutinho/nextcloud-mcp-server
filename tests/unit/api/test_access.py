"""Unit tests for access.py REST API endpoints.

Tests the REST API endpoints for user access and scope management:
- GET /api/v1/users/{user_id}/access - Get user's provisioned access and scopes
- PATCH /api/v1/users/{user_id}/scopes - Update user's application-level scopes
- GET /api/v1/scopes - List all supported scopes
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

from nextcloud_mcp_server.api import passwords
from nextcloud_mcp_server.api.access import (
    get_user_access,
    list_supported_scopes,
    update_user_scopes,
)
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_rate_limit():
    """Isolate the module-global rate-limiter state between tests."""
    passwords._rate_limit_attempts.clear()
    yield
    passwords._rate_limit_attempts.clear()


@pytest.fixture(autouse=True)
def mock_nextcloud_validation(mocker):
    """Stub the OCS credential check to succeed as user "alice".

    Every management endpoint now authenticates the BasicAuth password against
    Nextcloud's ``/cloud/user`` endpoint (GHSA-x88r-fhx7-52h6) — a matching
    username alone is no longer sufficient. These tests all target "alice", so
    return HTTP 200 with ``id == "alice"`` to pass the password + UID-ownership
    checks. Tests asserting 401/403 short-circuit before this stub is reached
    (missing header / username-path mismatch), so it is safe as autouse.
    """
    mocker.patch(
        "nextcloud_mcp_server.api.passwords.get_settings",
        return_value=MagicMock(
            nextcloud_host="http://localhost:8080",
            nextcloud_verify_ssl=True,
            nextcloud_ca_bundle=None,
        ),
    )
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "ocs": {"meta": {"statuscode": 200}, "data": {"id": "alice"}}
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.api.passwords.nextcloud_httpx_client",
        return_value=mock_client,
    )


@pytest.fixture
def encryption_key():
    """Generate a test encryption key."""
    return Fernet.generate_key().decode()


@pytest.fixture
async def temp_storage(encryption_key):
    """Create temporary storage instance with encryption for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_access.db"
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
    """Create a test Starlette app with the access endpoints."""
    app = Starlette(
        routes=[
            Route(
                "/api/v1/users/{user_id}/access",
                get_user_access,
                methods=["GET"],
            ),
            Route(
                "/api/v1/users/{user_id}/scopes",
                update_user_scopes,
                methods=["PATCH"],
            ),
            Route(
                "/api/v1/scopes",
                list_supported_scopes,
                methods=["GET"],
            ),
        ],
    )
    app.state.storage = storage
    return app


class TestGetUserAccess:
    """Tests for GET /api/v1/users/{user_id}/access."""

    async def test_not_provisioned(self, temp_storage):
        """Returns provisioned=False when no app password stored."""
        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.get(
            "/api/v1/users/alice/access",
            headers={"Authorization": create_basic_auth_header("alice", "pw")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["provisioned"] is False
        assert data["scopes"] is None

    async def test_provisioned_with_scopes(self, temp_storage):
        """Returns provisioned=True with scopes when app password exists."""
        await temp_storage.store_app_password_with_scopes(
            user_id="alice",
            app_password="test-app-pw",
            scopes=["notes.read", "calendar.write"],
            username="alice_nc",
        )

        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.get(
            "/api/v1/users/alice/access",
            headers={"Authorization": create_basic_auth_header("alice", "pw")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["provisioned"] is True
        assert set(data["scopes"]) == {"notes.read", "calendar.write"}
        assert data["username"] == "alice_nc"

    async def test_missing_auth_header(self, temp_storage):
        """Returns 401 when no Authorization header."""
        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.get("/api/v1/users/alice/access")
        assert resp.status_code == 401

    async def test_user_id_mismatch(self, temp_storage):
        """Returns 403 when path user_id doesn't match auth credentials."""
        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.get(
            "/api/v1/users/alice/access",
            headers={"Authorization": create_basic_auth_header("bob", "pw")},
        )
        assert resp.status_code == 403


class TestUpdateUserScopes:
    """Tests for PATCH /api/v1/users/{user_id}/scopes."""

    async def test_update_valid_scopes(self, temp_storage):
        """Successfully updates scopes for a provisioned user."""
        await temp_storage.store_app_password_with_scopes(
            user_id="alice",
            app_password="test-app-pw",
            scopes=["notes.read"],
            username="alice_nc",
        )

        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.patch(
            "/api/v1/users/alice/scopes",
            headers={"Authorization": create_basic_auth_header("alice", "pw")},
            json={"scopes": ["notes.read", "notes.write", "calendar.read"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert set(data["scopes"]) == {"notes.read", "notes.write", "calendar.read"}

    async def test_invalid_scopes(self, temp_storage):
        """Returns 400 for invalid scope names."""
        await temp_storage.store_app_password_with_scopes(
            user_id="alice",
            app_password="test-app-pw",
            scopes=["notes.read"],
        )

        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.patch(
            "/api/v1/users/alice/scopes",
            headers={"Authorization": create_basic_auth_header("alice", "pw")},
            json={"scopes": ["notes.read", "invalid:scope"]},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "invalid:scope" in data["error"]

    async def test_user_not_provisioned(self, temp_storage):
        """Returns 404 when user has no app password."""
        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.patch(
            "/api/v1/users/alice/scopes",
            headers={"Authorization": create_basic_auth_header("alice", "pw")},
            json={"scopes": ["notes.read"]},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert data["success"] is False

    async def test_missing_scopes_field(self, temp_storage):
        """Returns 400 when scopes field is missing from body."""
        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.patch(
            "/api/v1/users/alice/scopes",
            headers={"Authorization": create_basic_auth_header("alice", "pw")},
            json={"something_else": True},
        )
        assert resp.status_code == 400

    async def test_invalid_json_body(self, temp_storage):
        """Returns 400 for invalid JSON body."""
        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.patch(
            "/api/v1/users/alice/scopes",
            headers={
                "Authorization": create_basic_auth_header("alice", "pw"),
                "Content-Type": "application/json",
            },
            content=b"not json",
        )
        assert resp.status_code == 400

    async def test_non_dict_json_body(self, temp_storage):
        """Returns 400 (not 500) for a valid JSON body that isn't an object."""
        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.patch(
            "/api/v1/users/alice/scopes",
            headers={"Authorization": create_basic_auth_header("alice", "pw")},
            json=["notes.read"],  # a list, not an object
        )
        assert resp.status_code == 400
        assert resp.json()["success"] is False


class TestListSupportedScopes:
    """Tests for GET /api/v1/scopes."""

    async def test_returns_all_scopes(self, temp_storage):
        """Returns all supported scopes sorted."""
        app = create_test_app(temp_storage)
        client = TestClient(app)

        resp = client.get("/api/v1/scopes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert set(data["scopes"]) == ALL_SUPPORTED_SCOPES
        # Verify it's sorted
        assert data["scopes"] == sorted(data["scopes"])
