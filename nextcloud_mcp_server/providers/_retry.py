"""Shared rate-limit retry helper for provider modules.

OpenAI and Mistral both retry on 429 with the same exponential-backoff curve;
extracting the loop here keeps the two provider modules thin and lets future
providers (Bedrock throttling, etc.) reuse the same primitive.
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


def retry_on_rate_limit(
    exception_type: type[BaseException],
    is_rate_limit: Callable[[BaseException], bool] = lambda _exc: True,
    *,
    provider_name: str = "provider",
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Build a decorator that retries on rate-limit exceptions.

    Args:
        exception_type: Catch this exception class (e.g. ``openai.RateLimitError``,
            ``mistralai.client.errors.SDKError``).
        is_rate_limit: Predicate that decides whether a caught exception is
            actually a rate-limit (vs. some other error of the same class).
            Defaults to "always True" — appropriate when ``exception_type`` is
            already a rate-limit-specific class.
        provider_name: Used in log messages so operators can tell which
            provider exhausted retries.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            retry_delay = INITIAL_RETRY_DELAY
            last_error: BaseException | None = None

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    return await func(*args, **kwargs)
                except exception_type as e:
                    if not is_rate_limit(e):
                        raise
                    last_error = e
                    if attempt < MAX_RETRIES:
                        logger.warning(
                            "%s rate limit hit (attempt %d/%d), retrying in %.1fs...",
                            provider_name,
                            attempt,
                            MAX_RETRIES,
                            retry_delay,
                        )
                        await anyio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)

            logger.error(
                "%s rate limit exceeded after %d attempts", provider_name, MAX_RETRIES
            )
            if last_error is None:  # pragma: no cover — loop above always sets this
                raise RuntimeError("retry loop exited without capturing an error")
            raise last_error

        return wrapper

    return decorator
