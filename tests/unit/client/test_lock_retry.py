"""Unit tests for the 423 Locked retry policy in the shared transport.

Diagnosed from the CI failure this exists to fix (Deck card 1066). A
login-flow lane failed ``nc_notes_create_note`` with 423, and Nextcloud's own
log named the cause exactly::

    "/admin/files/Notes/LoginFlowTest" is locked, existing lock on file: 2 shared locks
      View::changeLock('/admin/files/Notes/LoginFlowTest', 2)
      View::basicOperation('mkdir', ...)
      Folder::newFolder('/admin/files/Notes/LoginFlowTest')

So the 423 was not the note file at all: it was the note's *category folder*,
failing to upgrade a shared lock to an exclusive one while a concurrent reader
held one. That is contention, not a refusal, and it clears in milliseconds --
which is what makes a short bounded retry the right response rather than a
blanket one.

The two budgets are tested separately because the whole point is that they do
not share: a lock wait must not spend a rate-limit attempt, or a 429 arriving
after a lock race would give up early.
"""

from __future__ import annotations

import pytest
from httpx import HTTPStatusError, Request, Response

from nextcloud_mcp_server.client.base import (
    LOCK_MAX_RETRIES,
    MAX_RETRIES,
    retry_on_429,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def no_sleep(mocker):
    """Keep the backoffs from actually elapsing."""
    return mocker.patch("nextcloud_mcp_server.client.base.anyio.sleep")


def _status_error(status: int) -> HTTPStatusError:
    request = Request("POST", "https://nc.example.com/apps/notes/api/v1/notes")
    return HTTPStatusError(
        str(status), request=request, response=Response(status, request=request)
    )


def _failing_then(statuses: list[int], calls: list[int]):
    """A callable raising each status in turn, then returning a sentinel."""

    async def call():
        calls.append(1)
        index = len(calls) - 1
        if index < len(statuses):
            raise _status_error(statuses[index])
        return "ok"

    return retry_on_429(call)


async def test_transient_lock_is_retried_and_succeeds(no_sleep):
    """The observed failure: one contended attempt, then the lock clears."""
    calls: list[int] = []

    assert await _failing_then([423], calls)() == "ok"
    assert len(calls) == 2
    assert no_sleep.call_count == 1


async def test_lock_backoff_is_short_and_doubling(no_sleep):
    """423 waits sub-second and doubles, unlike 429's flat 5s.

    A lock upgrade clears in milliseconds; waiting 5s for it would turn a
    recoverable blip into a visible stall on every contended write.
    """
    calls: list[int] = []

    assert await _failing_then([423, 423, 423], calls)() == "ok"
    assert [c.args[0] for c in no_sleep.call_args_list] == [0.5, 1.0, 2.0]


async def test_a_persistent_lock_still_surfaces_as_423(no_sleep):
    """The budget must not convert a real refusal into an unbounded wait.

    A file locked by a long upload or an open editing session is genuinely
    unavailable. Once the retries are spent the caller gets the original 423 --
    not a retry-exhausted RuntimeError, which would hide what happened.
    """
    calls: list[int] = []

    with pytest.raises(HTTPStatusError) as exc_info:
        await _failing_then([423] * 10, calls)()

    assert exc_info.value.response.status_code == 423
    assert len(calls) == LOCK_MAX_RETRIES + 1


async def test_lock_retries_do_not_spend_the_rate_limit_budget(no_sleep):
    """A 429 arriving after a lock race must still get its full budget.

    This is the reason the two counters are separate. Sharing one would let a
    contended write eat attempts a later 429 needs, so the rate-limit failure
    would arrive early and for the wrong reason.
    """
    calls: list[int] = []
    # Every lock retry, then rate limiting for the rest.
    tool = _failing_then([423] * LOCK_MAX_RETRIES + [429] * 20, calls)

    with pytest.raises(RuntimeError, match="Maximum number of retries"):
        await tool()

    assert len(calls) == LOCK_MAX_RETRIES + MAX_RETRIES


async def test_rate_limit_budget_is_unchanged(no_sleep):
    """The pre-existing 429 accounting must survive the restructure.

    Five attempts, each followed by a wait, then RuntimeError -- the shape
    every existing caller and test already depends on.
    """
    calls: list[int] = []

    with pytest.raises(RuntimeError, match="Maximum number of retries"):
        await _failing_then([429] * 20, calls)()

    assert len(calls) == MAX_RETRIES
    assert no_sleep.call_count == MAX_RETRIES


@pytest.mark.parametrize("status", [400, 403, 404, 409, 500, 507])
async def test_other_statuses_are_not_retried(no_sleep, status):
    """Only "not now" is retried. Everything else is a decision, not a delay."""
    calls: list[int] = []

    with pytest.raises(HTTPStatusError):
        await _failing_then([status] * 5, calls)()

    assert len(calls) == 1
    no_sleep.assert_not_called()
