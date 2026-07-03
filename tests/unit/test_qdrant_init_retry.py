"""Unit tests for the startup Qdrant-collection-init retry.

Qdrant can be briefly unreachable during a rolling deploy; the startup path
retries transient connection failures (capped backoff + jitter) instead of
crashlooping with a full traceback, while genuine misconfigurations still fail
fast. See ``nextcloud_mcp_server.app._init_qdrant_collection_with_retry``.
"""

import types

import httpx
import pytest
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from nextcloud_mcp_server.app import (
    _init_qdrant_collection_with_retry,
    _qdrant_init_error_is_transient,
)


@pytest.mark.unit
class TestQdrantInitErrorIsTransient:
    def test_httpx_transport_error_is_transient(self):
        assert _qdrant_init_error_is_transient(httpx.ConnectError("refused")) is True
        assert _qdrant_init_error_is_transient(httpx.ReadTimeout("slow")) is True

    def test_qdrant_response_handling_exception_is_transient(self):
        exc = ResponseHandlingException(httpx.ConnectError("refused"))
        assert _qdrant_init_error_is_transient(exc) is True

    def test_transient_cause_is_detected_through_chain(self):
        outer = RuntimeError("wrapper")
        outer.__cause__ = httpx.ConnectError("refused")
        assert _qdrant_init_error_is_transient(outer) is True

    def test_config_error_is_not_transient(self):
        assert _qdrant_init_error_is_transient(ValueError("bad url")) is False

    def test_4xx_status_error_is_not_transient(self):
        # A 4xx (auth/config) surfaces as UnexpectedResponse — fail fast.
        exc = UnexpectedResponse(
            status_code=403,
            reason_phrase="Forbidden",
            content=b"",
            headers=httpx.Headers(),
        )
        assert _qdrant_init_error_is_transient(exc) is False

    def test_5xx_status_error_is_transient(self):
        # A 5xx from a reachable-but-overloaded/starting Qdrant is transient.
        exc = UnexpectedResponse(
            status_code=503,
            reason_phrase="Service Unavailable",
            content=b"",
            headers=httpx.Headers(),
        )
        assert _qdrant_init_error_is_transient(exc) is True


def _settings(max_attempts: int):
    return types.SimpleNamespace(
        qdrant_init_max_attempts=max_attempts,
        qdrant_init_backoff_base=0.0,
        qdrant_init_backoff_max=0.0,
    )


@pytest.mark.unit
class TestInitQdrantCollectionWithRetry:
    async def test_retries_transient_then_succeeds(self, mocker):
        sleep = mocker.patch("nextcloud_mcp_server.app.anyio.sleep")
        mocker.patch("nextcloud_mcp_server.app.get_settings", return_value=_settings(5))
        get_client = mocker.patch(
            "nextcloud_mcp_server.app.get_qdrant_client",
            side_effect=[
                ResponseHandlingException(httpx.ConnectError("refused")),
                httpx.ConnectError("refused"),
                mocker.AsyncMock(),  # success on the 3rd attempt
            ],
        )

        await _init_qdrant_collection_with_retry()

        assert get_client.call_count == 3
        assert sleep.await_count == 2  # slept before each retry, not after success

    async def test_non_transient_fails_fast_without_retry(self, mocker):
        sleep = mocker.patch("nextcloud_mcp_server.app.anyio.sleep")
        mocker.patch("nextcloud_mcp_server.app.get_settings", return_value=_settings(5))
        get_client = mocker.patch(
            "nextcloud_mcp_server.app.get_qdrant_client",
            side_effect=ValueError("bad api key"),
        )

        with pytest.raises(RuntimeError, match="Qdrant initialization failed"):
            await _init_qdrant_collection_with_retry()

        assert get_client.call_count == 1  # no retry on a genuine error
        sleep.assert_not_awaited()

    async def test_transient_exhausts_budget_then_raises(self, mocker):
        mocker.patch("nextcloud_mcp_server.app.anyio.sleep")
        mocker.patch("nextcloud_mcp_server.app.get_settings", return_value=_settings(2))
        get_client = mocker.patch(
            "nextcloud_mcp_server.app.get_qdrant_client",
            side_effect=httpx.ConnectError("refused"),
        )

        with pytest.raises(RuntimeError, match="Qdrant initialization failed"):
            await _init_qdrant_collection_with_retry()

        assert get_client.call_count == 2  # bounded by qdrant_init_max_attempts
