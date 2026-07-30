"""Unit tests for the shared transient-error retry decorator."""

from unittest.mock import AsyncMock

import pytest

from nextcloud_mcp_server import retry as _retry


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

    @_retry.retry_on_transient(_FakeError, should_retry=lambda e: e.status_code == 429)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeError(429)
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.unit
async def test_retry_reraises_when_predicate_returns_false():
    """An error the predicate rejects is re-raised on first hit (no retry)."""
    calls = {"n": 0}

    @_retry.retry_on_transient(_FakeError, should_retry=lambda e: e.status_code == 429)
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

    @_retry.retry_on_transient(_FakeError, should_retry=lambda e: e.status_code == 429)
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

    @_retry.retry_on_transient(_FakeError)
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

    @_retry.retry_on_transient(_FakeError)
    async def value_error():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await value_error()


class _ConnError(Exception):
    pass


@pytest.mark.unit
async def test_retry_accepts_tuple_of_exception_types():
    """A tuple of exception classes is caught (the OpenAI transient set shape)."""
    calls = {"n": 0}

    @_retry.retry_on_transient((_FakeError, _ConnError))
    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ConnError("dropped")
        if calls["n"] == 2:
            raise _FakeError(503)
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


@pytest.mark.unit
async def test_max_delay_caps_the_first_retry_too():
    """``initial_delay`` above ``max_delay`` is clamped from the first sleep on.

    No validator enforces base <= max at the call sites (app.py threads the
    ``*_BACKOFF_BASE``/``*_BACKOFF_MAX`` settings straight through), so an
    operator can invert them. Without the clamp the first sleep would be the
    un-capped ``initial_delay``.
    """

    @_retry.retry_on_transient(
        _FakeError, max_retries=3, initial_delay=30.0, max_delay=5.0
    )
    async def always_fails():
        raise _FakeError(503)

    with pytest.raises(_FakeError):
        await always_fails()

    delays = [call.args[0] for call in _retry.anyio.sleep.await_args_list]
    assert delays == [5.0, 5.0]  # Capped immediately, not 30.0 then 5.0.


@pytest.mark.unit
async def test_max_delay_caps_jittered_sleeps():
    """With jitter the draw is bounded by ``max_delay``, not ``initial_delay``."""

    @_retry.retry_on_transient(
        _FakeError, max_retries=4, initial_delay=30.0, max_delay=5.0, jitter=True
    )
    async def always_fails():
        raise _FakeError(503)

    with pytest.raises(_FakeError):
        await always_fails()

    delays = [call.args[0] for call in _retry.anyio.sleep.await_args_list]
    assert len(delays) == 3
    assert all(0.0 <= d <= 5.0 for d in delays), delays
