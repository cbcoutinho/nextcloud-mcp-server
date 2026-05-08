"""Unit tests for the shared rate-limit retry decorator."""

from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server.providers import _retry


class _FakeError(Exception):
    """Stand-in for an SDK exception with an HTTP status code attached."""

    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Replace anyio.sleep with an awaitable no-op so retries don't waste time."""
    monkeypatch.setattr(_retry.anyio, "sleep", AsyncMock(return_value=None))


@pytest.mark.unit
async def test_retry_succeeds_after_429():
    """A 429 followed by success returns the success value."""
    calls = {"n": 0}

    @_retry.retry_on_rate_limit(
        _FakeError, is_rate_limit=lambda e: e.status_code == 429
    )
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeError(429)
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.unit
async def test_retry_reraises_non_rate_limit_immediately():
    """A non-rate-limit error of the same class is re-raised on first hit."""
    calls = {"n": 0}

    @_retry.retry_on_rate_limit(
        _FakeError, is_rate_limit=lambda e: e.status_code == 429
    )
    async def boom():
        calls["n"] += 1
        raise _FakeError(500)

    with pytest.raises(_FakeError, match="status 500"):
        await boom()
    assert calls["n"] == 1  # No retries on non-429.


@pytest.mark.unit
async def test_retry_gives_up_after_max_retries():
    """After MAX_RETRIES failed attempts the last error is re-raised."""
    calls = {"n": 0}

    @_retry.retry_on_rate_limit(
        _FakeError, is_rate_limit=lambda e: e.status_code == 429
    )
    async def always_429():
        calls["n"] += 1
        raise _FakeError(429)

    with pytest.raises(_FakeError, match="status 429"):
        await always_429()
    assert calls["n"] == _retry.MAX_RETRIES


@pytest.mark.unit
async def test_retry_default_predicate_treats_all_as_rate_limit():
    """Default predicate (`lambda _: True`) retries every caught exception."""
    calls = {"n": 0}

    @_retry.retry_on_rate_limit(_FakeError)
    async def fail_once():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _FakeError(503)
        return "recovered"

    result = await fail_once()
    assert result == "recovered"
    assert calls["n"] == 2


@pytest.mark.unit
async def test_retry_does_not_catch_unrelated_exceptions():
    """Exceptions of a different class bypass the decorator entirely."""

    @_retry.retry_on_rate_limit(_FakeError)
    async def value_error():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await value_error()
