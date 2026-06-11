"""Shared transient-error retry helper for provider modules.

OpenAI and Mistral retry transient failures (429 rate limits, plus connection
drops / timeouts / 5xx for the embedding path) on the same exponential-backoff
curve; extracting the loop here keeps the two provider modules thin and lets
future providers (Bedrock throttling, etc.) reuse the same primitive. The
``should_retry`` predicate decides which caught exceptions are transient.
"""

from __future__ import annotations

import logging
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
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Build a decorator that retries transient exceptions with backoff.

    Args:
        exception_type: Catch this exception class (or tuple of classes), e.g.
            ``openai.APIError`` or ``mistralai.client.errors.SDKError``.
        should_retry: Predicate that decides whether a caught exception is
            transient (and so retryable) vs. a permanent error of the same
            class. Defaults to "always True" — appropriate when
            ``exception_type`` is already transient-specific (e.g. a 429 class).
        provider_name: Used in log messages so operators can tell which
            provider exhausted retries.
        label: Short noun for the log message ("rate limit", "transient error")
            so the line accurately names what was retried.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            retry_delay = INITIAL_RETRY_DELAY
            last_error: BaseException | None = None

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    return await func(*args, **kwargs)
                # exception_type is constrained by the signature to a
                # BaseException subclass or a tuple of them; the dynamic catch is
                # the whole point of this reusable helper.
                except exception_type as e:  # NOSONAR(S5708)
                    if not should_retry(e):
                        raise
                    last_error = e
                    if attempt < MAX_RETRIES:
                        logger.warning(
                            "%s %s (attempt %d/%d): %r; retrying in %.1fs...",
                            provider_name,
                            label,
                            attempt,
                            MAX_RETRIES,
                            e,
                            retry_delay,
                        )
                        await anyio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)

            logger.error(
                "%s %s not resolved after %d attempts: %r",
                provider_name,
                label,
                MAX_RETRIES,
                last_error,
            )
            if last_error is None:  # pragma: no cover — loop above always sets this
                raise RuntimeError("retry loop exited without capturing an error")
            raise last_error

        return wrapper

    return decorator


# Back-compat alias: the helper was originally rate-limit-specific.
retry_on_rate_limit = retry_on_transient
