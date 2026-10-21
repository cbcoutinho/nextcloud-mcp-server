"""The broker stores rotated refresh tokens with the IdP's lifetime (#411).

Storage treats ``expires_at`` as authoritative: ``get_refresh_token`` deletes
and refuses a token past it, and ``cleanup_expired_tokens`` sweeps them. The
broker used to stamp every rotated token with a fixed 90 days, so a token the
IdP expires sooner (Nextcloud's ``oidc`` app defaults to 900 seconds) stayed
"valid" in storage and kept being sent to the IdP, and one the IdP keeps
longer was thrown away at 90 days.
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage
from nextcloud_mcp_server.auth.token_broker import TokenBrokerService

pytestmark = pytest.mark.unit

_DAY = 86400
_OIDC_CONFIG = {
    "issuer": "https://idp.example.com",
    "token_endpoint": "https://idp.example.com/token",
}


def _broker(storage) -> TokenBrokerService:
    return TokenBrokerService(
        storage=storage,
        oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
        nextcloud_host="https://nextcloud.example.com",
        client_id="test_client_id",
        client_secret="test_client_secret",
    )


def _token_response(**extra) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": "new_access",
        "refresh_token": "rotated_refresh",
        "expires_in": 3600,
        "token_type": "Bearer",
        **extra,
    }
    return response


async def _rotate_via(path: str, broker: TokenBrokerService) -> None:
    """Drive one of the three code paths that store a rotated refresh token."""
    if path == "refresh_access_token":
        await broker._refresh_access_token("old_refresh", user_id="user1")
    elif path == "refresh_access_token_with_scopes":
        await broker._refresh_access_token_with_scopes(
            "old_refresh", ["notes.read"], user_id="user1"
        )
    else:
        await broker.refresh_master_token("user1")


_PATHS = [
    "refresh_access_token",
    "refresh_access_token_with_scopes",
    "refresh_master_token",
]


async def _stored_lifetime(path: str, **token_fields) -> int:
    """Seconds between now and the expires_at the broker stored."""
    storage = AsyncMock()
    storage.get_refresh_token = AsyncMock(
        return_value={"refresh_token": "old_refresh", "expires_at": None}
    )
    storage.store_refresh_token = AsyncMock()
    broker = _broker(storage)
    client = AsyncMock()
    client.post.return_value = _token_response(**token_fields)
    try:
        with (
            patch.object(broker, "_get_oidc_config", return_value=_OIDC_CONFIG),
            patch.object(broker, "_get_http_client", return_value=client),
        ):
            before = int(time.time())
            await _rotate_via(path, broker)
    finally:
        await broker.close()
    storage.store_refresh_token.assert_called_once()
    expires_at = storage.store_refresh_token.call_args.kwargs["expires_at"]
    assert isinstance(expires_at, int)
    return expires_at - before


@pytest.mark.parametrize("path", _PATHS)
class TestIdPLifetimeIsUsed:
    async def test_short_lifetime_is_honoured(self, path):
        """Nextcloud oidc's default: 15 minutes, not 90 days."""
        assert await _stored_lifetime(path, refresh_expires_in=900) in (900, 901)

    async def test_lifetime_longer_than_90_days_is_honoured(self, path):
        lifetime = await _stored_lifetime(path, refresh_expires_in=200 * _DAY)
        assert lifetime in (200 * _DAY, 200 * _DAY + 1)

    async def test_numeric_string_is_accepted(self, path):
        """Some IdPs (e.g. AWS Cognito) send the number as a JSON string."""
        lifetime = await _stored_lifetime(path, refresh_expires_in="604800")
        assert lifetime in (604800, 604801)

    async def test_float_is_truncated_to_seconds(self, path):
        lifetime = await _stored_lifetime(path, refresh_expires_in=3600.9)
        assert lifetime in (3600, 3601)

    async def test_effectively_never_is_capped_at_ten_years(self, path):
        lifetime = await _stored_lifetime(path, refresh_expires_in=10**15)
        assert lifetime in (3650 * _DAY, 3650 * _DAY + 1)


@pytest.mark.parametrize("path", _PATHS)
class TestFallbackToNinetyDays:
    """Regression guards: anything that is not a positive lifetime keeps the
    90-day default the broker has always used."""

    async def test_absent(self, path):
        assert await _stored_lifetime(path) in (90 * _DAY, 90 * _DAY + 1)

    @pytest.mark.parametrize(
        "value",
        [
            0,
            -60,
            None,
            True,
            "",
            "abc",
            "1.5",
            "-900",
            float("nan"),
            float("inf"),
            {"s": 900},
        ],
        ids=repr,
    )
    async def test_unusable_value(self, path, value):
        lifetime = await _stored_lifetime(path, refresh_expires_in=value)
        assert lifetime in (90 * _DAY, 90 * _DAY + 1)


class TestStorageHonoursTheStoredExpiry:
    """End to end with real SQLite storage: the expiry the broker writes is
    the one ``get_refresh_token`` enforces."""

    @pytest.fixture
    async def storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = RefreshTokenStorage(
                db_path=str(Path(tmpdir) / "tokens.db"),
                encryption_key=Fernet.generate_key().decode(),
            )
            await storage.initialize()
            yield storage

    async def _rotate(self, storage, refresh_expires_in) -> float:
        broker = _broker(storage)
        client = AsyncMock()
        client.post.return_value = _token_response(
            refresh_expires_in=refresh_expires_in
        )
        try:
            with (
                patch.object(broker, "_get_oidc_config", return_value=_OIDC_CONFIG),
                patch.object(broker, "_get_http_client", return_value=client),
            ):
                now = time.time()
                await broker._refresh_access_token_with_scopes(
                    "old_refresh", ["notes.read"], user_id="user1"
                )
        finally:
            await broker.close()
        return now

    async def test_token_past_the_idp_lifetime_is_not_returned(self, storage, mocker):
        now = await self._rotate(storage, 900)
        clock = mocker.patch("nextcloud_mcp_server.auth.storage.time")
        clock.time.return_value = now + 905
        assert await storage.get_refresh_token("user1") is None

    async def test_token_within_the_idp_lifetime_is_returned(self, storage, mocker):
        now = await self._rotate(storage, 900)
        clock = mocker.patch("nextcloud_mcp_server.auth.storage.time")
        clock.time.return_value = now + 800
        stored = await storage.get_refresh_token("user1")
        assert stored is not None
        assert stored["refresh_token"] == "rotated_refresh"
