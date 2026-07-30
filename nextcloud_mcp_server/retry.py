"""Shared transient-error retry helper.

One capped exponential-backoff loop, used by anything that needs to ride out a
transient failure: the embedding providers (429 rate limits, plus connection
drops / timeouts / 5xx for the embedding path), the startup dependency probes
(OIDC discovery, Qdrant collection init), and the ingest indexing retry. The
``should_retry`` predicate decides which caught exceptions are transient;
everything else propagates immediately.

``jitter=True`` adds full jitter (``uniform(0, delay)``) on top of the capped
exponential curve, which is what the startup probes want — without it, every
replica in a deployment retries a briefly-unreachable dependency in lockstep.

Not every retry loop in this codebase belongs here, and the ones that stayed
put did so for a reason:

- ``BaseNextcloudClient._stream_request`` — a partially-consumed stream cannot
  be replayed, and it meters every attempt on the API-call histogram.
- ``CalendarClient._wait_for_calendar`` — a poll-until-a-condition-holds loop.
  It retries a call that *succeeded* but hasn't converged yet, which an
  exception-driven decorator cannot express without inventing a sentinel.
- ``client_registration`` — inspects ``response.status_code`` directly and
  honours ``Retry-After`` as ``min(header, local_backoff)``. Expressing that
  would mean a hook parameter here serving exactly one caller.
- ``vector/processor`` indexing retry — its per-attempt log carries structured
  ``extra`` fields (doc_id, doc_type, attempt, status) that operators query on,
  and its exhausted branch derives a drop reason and emits two metrics. Neither
  is expressible through a generic decorator.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

import anyio

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 2.0
MAX_RETRY_DELAY = 60.0

T = TypeVar("T")


def retry_on_transient(
    exception_type: type[BaseException] | tuple[type[BaseException], ...],
    should_retry: Callable[[BaseException], bool] = lambda _exc: True,
    *,
    provider_name: str = "provider",
    label: str = "rate limit",
    max_retries: int = MAX_RETRIES,
    initial_delay: float = INITIAL_RETRY_DELAY,
    max_delay: float = MAX_RETRY_DELAY,
    jitter: bool = False,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Build a decorator that retries transient exceptions with backoff.

    Args:
        exception_type: Catch this exception class (or tuple of classes), e.g.
            ``openai.APIError`` or ``httpx.RequestError``.
        should_retry: Predicate that decides whether a caught exception is
            transient (and so retryable) vs. a permanent error of the same
            class. Defaults to "always True" — appropriate when
            ``exception_type`` is already transient-specific (e.g. a 429 class).
        provider_name: Used in log messages so operators can tell which caller
            exhausted retries.
        label: Short noun for the log message ("rate limit", "transient error")
            so the line accurately names what was retried.
        max_retries: Total attempts, including the first. Clamped to at least 1,
            so a caller threading a settings value through (where a validator
            may have been bypassed) still runs once instead of falling straight
            to the exhausted branch with nothing to raise.
        initial_delay: Delay before the second attempt, doubled thereafter.
        max_delay: Ceiling on every sleep, the first one included — so passing
            ``initial_delay`` greater than this clamps rather than overshoots.
        jitter: Sleep ``uniform(0, delay)`` rather than exactly ``delay``, so
            concurrent callers spread out instead of retrying in lockstep.
    """

    attempts = max(1, max_retries)

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await _run_with_retries(
                lambda: func(*args, **kwargs),
                exception_type,
                should_retry,
                provider_name=provider_name,
                label=label,
                attempts=attempts,
                initial_delay=initial_delay,
                max_delay=max_delay,
                jitter=jitter,
            )

        return wrapper

    return decorator


async def _run_with_retries(
    call: Callable[[], Awaitable[T]],
    exception_type: type[BaseException] | tuple[type[BaseException], ...],
    should_retry: Callable[[BaseException], bool],
    *,
    provider_name: str,
    label: str,
    attempts: int,
    initial_delay: float,
    max_delay: float,
    jitter: bool,
) -> T:
    """Run ``call``, retrying transient failures with capped exponential backoff.

    Lives at module level rather than nested inside ``retry_on_transient`` so the
    loop reads at one indent level instead of four.
    """
    # Clamp the first delay too, not just the doubled ones: ``max_delay`` is a
    # ceiling on every sleep, and no validator enforces base <= max at the call
    # sites. Matches the per-call-site loops this helper replaced, which
    # computed min(max, base * 2 ** (attempt - 1)) from the first attempt on.
    retry_delay = min(initial_delay, max_delay)
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await call()
        # exception_type is constrained by the signature to a BaseException
        # subclass or a tuple of them; the dynamic catch is the whole point of
        # this reusable helper.
        except exception_type as e:  # NOSONAR(S5708)
            if not should_retry(e):
                raise
            last_error = e

        if attempt < attempts:
            sleep_for = random.uniform(0, retry_delay) if jitter else retry_delay
            logger.warning(
                "%s %s (attempt %d/%d): %r; retrying in %.1fs...",
                provider_name,
                label,
                attempt,
                attempts,
                last_error,
                sleep_for,
            )
            await anyio.sleep(sleep_for)
            retry_delay = min(retry_delay * 2, max_delay)

    logger.error(
        "%s %s not resolved after %d attempts: %r",
        provider_name,
        label,
        attempts,
        last_error,
    )
    if last_error is None:  # pragma: no cover — loop above always sets this
        raise RuntimeError("retry loop exited without capturing an error")
    raise last_error
