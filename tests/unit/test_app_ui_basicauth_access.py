"""The /app browser UI must not hand out admin access without a credential.

`SessionAuthBackend` used to return ``["authenticated", "admin"]`` for *any*
caller whenever OAuth was off — no cookie, no header, nothing. `/app` is mounted
unconditionally in every deployment mode, so in multi-user BasicAuth that meant
anyone reaching the port got the admin UI (webhook presets, vector-viz search
over the indexed corpus, revoke), attributed to ``cfg("NEXTCLOUD_USERNAME",
"admin")`` — a user that mode forbids configuring, so literally ``"admin"``.

Single-user keeps the pass-through: the server holds exactly one identity and
every request already acts as it, so there is nothing to distinguish callers by.
That is the deployment model, guarded by a startup warning instead.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.authentication import SimpleUser

from nextcloud_mcp_server.auth.session_backend import SessionAuthBackend
from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.config_validators import (
    AuthMode,
    detect_auth_mode,
    validate_configuration,
)

pytestmark = pytest.mark.unit


def _conn_without_credentials() -> SimpleNamespace:
    """An HTTPConnection-shaped object carrying no cookie and no auth header."""
    return SimpleNamespace(cookies={}, scope={"state": {}}, app=SimpleNamespace())


async def test_multi_user_basic_denies_app_without_session():
    """The bypass: no credential must mean no /app access in multi-user mode."""
    backend = SessionAuthBackend(oauth_enabled=False, multi_user_basic=True)

    assert await backend.authenticate(_conn_without_credentials()) is None


async def test_single_user_basic_still_passes_through():
    """Single-user is one identity by design; the guard is the startup warning."""
    backend = SessionAuthBackend(oauth_enabled=False, multi_user_basic=False)

    with patch("nextcloud_mcp_server.auth.session_backend.cfg", return_value="alice"):
        result = await backend.authenticate(_conn_without_credentials())

    assert result is not None
    credentials, user = result
    assert isinstance(user, SimpleUser)
    assert user.username == "alice"
    assert "admin" in credentials.scopes


async def test_multi_user_basic_denies_even_with_username_configured():
    """Belt and braces: the deny must not depend on NEXTCLOUD_USERNAME being unset.

    Config validation already hard-fails that combination (see
    ``test_multi_user_basic_rejects_configured_nextcloud_username``), but the
    backend must not rely on another layer having caught it.
    """
    backend = SessionAuthBackend(oauth_enabled=False, multi_user_basic=True)

    with patch("nextcloud_mcp_server.auth.session_backend.cfg", return_value="admin"):
        assert await backend.authenticate(_conn_without_credentials()) is None


def test_multi_user_basic_rejects_configured_nextcloud_username():
    """A single-user credential in multi-user mode is a misconfiguration.

    It signals an operator who thinks they configured one mode but got another,
    so startup refuses rather than silently ignoring the credential. Pinning the
    existing behaviour: ``nextcloud_username``/``nextcloud_password`` are in
    multi-user's ``forbidden`` list, and ``get_app`` raises on any config error.
    """
    settings = Settings(
        deployment_mode="multi_user_basic",
        nextcloud_host="http://nc.test",
        nextcloud_username="admin",
        nextcloud_password="secret",
    )

    mode = detect_auth_mode(settings)
    assert mode is AuthMode.MULTI_USER_BASIC

    _mode, errors = validate_configuration(settings)
    joined = " ".join(errors)
    assert "NEXTCLOUD_USERNAME" in joined
    assert "NEXTCLOUD_PASSWORD" in joined
