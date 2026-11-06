"""Parsers for operator- and peer-supplied scalar strings.

Each of these was previously re-implemented at several call sites with
diverging edge cases (``"1"`` enabling one flag but not another, ``int()`` on a
``Retry-After`` HTTP-date raising mid-retry). One definition, one property test.
"""

import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

_TRUE = frozenset({"1", "true", "yes", "on"})
# "" is an explicit false, not "unset": `FLAG=` in an env file means off.
_FALSE = frozenset({"0", "false", "no", "off", ""})


def parse_bool(value: Any, default: bool = False, *, name: str = "flag") -> bool:
    """Interpret a flag the way an operator would type it.

    Dynaconf only casts lowercase ``true``/``false``, so ``FLAG=False`` arrives
    as the string ``"False"`` and ``bool("False")`` is ``True``. Recognised
    spellings (any case, surrounding whitespace ignored) map to their meaning.
    ``None`` yields *default*; any other value also yields *default*, with a
    warning naming *name* so a typo such as ``ture`` is visible.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    word = str(value).strip().lower()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    _warn_unrecognised(name, repr(value), default)
    return default


@functools.cache
def _warn_unrecognised(name: str, value: str, default: bool) -> None:
    """Warn once per (setting, value): some flags are read on every request."""
    logger.warning(
        "Unrecognised boolean %s=%s; expected one of %s or %s, using %s",
        name,
        value,
        sorted(_TRUE),
        sorted(_FALSE - {""}),
        default,
    )


def split_csv(raw: str | None) -> list[str]:
    """Split a comma-separated value into stripped items, dropping blanks."""
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def parse_retry_after(value: str | None) -> int | None:
    """Parse ``Retry-After`` delta-seconds (RFC 9110 §10.2.3: ``1*DIGIT``).

    Returns a positive number of seconds, or None for an absent, zero,
    malformed or HTTP-date value, in which case the caller uses its own
    back-off.
    """
    raw = (value or "").strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    return int(raw) or None
